import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import os
import json
import re
import time
import fitz  # PyMuPDF
from google.oauth2 import service_account
from google import genai
from google.genai import types

import sys
import os

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)
    
# ==========================================
# AYARLAR VE YAPILANDIRMA
# ==========================================
LOCATION = "global"
CONFIG_FILE = os.path.expanduser("~/.subtitle_fixer_config.json")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(config_data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f)

# ==========================================
# PyMuPDF (PDF -> SRT) BELLEK İÇİ İŞLEME
# ==========================================
def extract_raw_lines_from_pdf(pdf_path, tolerance=8):
    """PDF'ten dikey koordinatlara göre metin satırlarını belleğe çıkarır."""
    doc = fitz.open(pdf_path)
    all_lines = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        words = page.get_text("words")
        if not words: continue

        words.sort(key=lambda w: (w[1], w[0]))
        current_line = []
        current_y0 = words[0][1]

        for w in words:
            if abs(w[1] - current_y0) <= tolerance:
                current_line.append(w)
            else:
                current_line.sort(key=lambda w: w[0])
                all_lines.append(" ".join([word[4] for word in current_line]))
                current_line = [w]
                current_y0 = w[1]
        
        if current_line:
            current_line.sort(key=lambda w: w[0])
            all_lines.append(" ".join([word[4] for word in current_line]))
    
    doc.close()
    return all_lines

def parse_and_clean_subtitles(raw_lines):
    """Bozuk sayıları temizler ve metinleri zaman damgalarıyla eşleştirir."""
    structured_blocks = []
    current_block = {"timestamp": None, "text_lines": []}
    
    timestamp_pattern = re.compile(r'(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})')
    standalone_number_pattern = re.compile(r'^\d+$')

    for line in raw_lines:
        line = line.strip()
        if not line: continue
        if standalone_number_pattern.match(line): continue

        ts_match = timestamp_pattern.search(line)
        if ts_match:
            if current_block["timestamp"] or current_block["text_lines"]:
                structured_blocks.append(current_block)
            
            timestamp = ts_match.group(1).replace('.', ',')
            text_part = line.replace(ts_match.group(0), "").strip()
            text_part = re.sub(r'^\d+\s+', '', text_part).strip()
            
            current_block = {"timestamp": timestamp, "text_lines": []}
            if text_part: current_block["text_lines"].append(text_part)
        else:
            current_block["text_lines"].append(line)

    if current_block["timestamp"] or current_block["text_lines"]:
        structured_blocks.append(current_block)

    return structured_blocks

def convert_pdf_to_srt_string(pdf_path):
    """Dosyaya yazmak yerine SRT yapısını doğrudan bir String olarak bellekte oluşturur."""
    raw_lines = extract_raw_lines_from_pdf(pdf_path)
    clean_blocks = parse_and_clean_subtitles(raw_lines)
    
    srt_content = ""
    for idx, block in enumerate(clean_blocks, start=1):
        timestamp = block["timestamp"] if block["timestamp"] else "00:00:00,000 --> 00:00:00,000"
        text = "\n".join(block["text_lines"])
        srt_content += f"{idx}\n{timestamp}\n{text}\n\n"
    
    return srt_content

def read_file_safely(filepath):
    encodings = ['utf-8-sig', 'utf-8', 'windows-1256', 'cp1252']
    for enc in encodings:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"{filepath} okunamadı. Lütfen dosyayı UTF-8 formatına dönüştürün.")

# ==========================================
# GUI (GÖRSEL ARAYÜZ) VE ANA UYGULAMA MANTIĞI
# ==========================================
class SubtitleFixerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Altyazı Düzeltici (Gemini AI)")
        self.root.geometry("750x600")
        self.root.configure(padx=20, pady=20)
        self.config = load_config()
        self.client = None

        # Stil Ayarları
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TButton", padding=6, font=('Segoe UI', 10))
        style.configure("TLabel", font=('Segoe UI', 10))
        style.configure("Header.TLabel", font=('Segoe UI', 12, 'bold'))

        # --- Arayüz Bileşenleri ---
        
        # Başlık ve Bilgi
        ttk.Label(root, text="Yapay Zeka Destekli Altyazı Düzenleyici", style="Header.TLabel").pack(pady=(0, 15))
        # Başlık ve Bilgi
        ttk.Label(root, text="PDF'den SRT'ye Dönüştürücü ve Altyazı Düzeltici", style="TLabel").pack(pady=(0, 10))

        # JSON Seçim Çerçevesi
        json_frame = ttk.Frame(root)
        json_frame.pack(fill='x', pady=5)
        
        self.lbl_json_status = ttk.Label(json_frame, text="Google JSON Anahtarı: Bekleniyor...", foreground="red")
        self.lbl_json_status.pack(side='left')
        
        ttk.Button(json_frame, text="JSON Anahtarı Seç", command=self.select_json_key).pack(side='right')

# ... (JSON Seçim Çerçevesi kısmı aynı kalıyor) ...

        # --- Genel (Dosya) İlerleme Çerçevesi ---
        overall_frame = ttk.Frame(root)
        overall_frame.pack(fill='x', pady=(15, 5))
        
        self.lbl_overall_status = ttk.Label(overall_frame, text="Bekleniyor... (0/0)", font=('Segoe UI', 10, 'bold'))
        self.lbl_overall_status.pack(side='top', anchor='w')
        
        self.overall_progress_bar = ttk.Progressbar(overall_frame, orient="horizontal", mode="determinate")
        self.overall_progress_bar.pack(fill='x', expand=True, pady=(5, 0))

        # --- Dosya İçi İlerleme Çerçevesi ---
        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill='x', pady=(5, 15))
        
        # Üstteki Büyük Dosya İsmi Metni
        self.lbl_current_file = ttk.Label(progress_frame, text="Şu Anki Dosya: -", font=('Segoe UI', 10, 'bold'), foreground="#005fb8")
        self.lbl_current_file.pack(side='top', anchor='w', pady=(0, 5))
        
        # Alt çerçeve (Sadece bar ve %100 metni yan yana dursun diye)
        bar_frame = ttk.Frame(progress_frame)
        bar_frame.pack(fill='x', expand=True)

        self.progress_bar = ttk.Progressbar(bar_frame, orient="horizontal", mode="determinate")
        self.progress_bar.pack(side='left', fill='x', expand=True)

        self.lbl_progress = ttk.Label(bar_frame, text="%0", font=('Segoe UI', 10, 'bold'))
        self.lbl_progress.pack(side='right', padx=(10, 0))

        # ... (Log Penceresi kısmı aynı kalıyor) ...
        # Log Penceresi
        ttk.Label(root, text="İşlem Geçmişi:").pack(anchor='w')
        self.log_area = scrolledtext.ScrolledText(root, wrap=tk.WORD, width=80, height=20, font=('Consolas', 9))
        self.log_area.pack(fill='both', expand=True, pady=(5, 15))
        self.log_area.config(state=tk.DISABLED)

        # Başlat Butonu
        self.btn_start = ttk.Button(root, text="Dosyaları Seç ve Başlat", command=self.start_processing_thread)
        self.btn_start.pack(fill='x', ipady=5)

        # Başlangıç kontrolleri
        self.check_saved_json()

    # --- Yardımcı Arayüz Metotları ---
    def log(self, message):
        """Arayüzdeki log penceresine mesaj ekler (Thread-safe)."""
        self.root.after(0, self._append_log, message)

    def _append_log(self, message):
        self.log_area.config(state=tk.NORMAL)
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.see(tk.END)
        self.log_area.config(state=tk.DISABLED)

    def update_progress(self, current, total):
        """İlerleme çubuğunu günceller."""
        percent = int((current / total) * 100) if total > 0 else 0
        def _update():
            self.progress_bar["maximum"] = total
            self.progress_bar["value"] = current
            self.lbl_progress.config(text=f"%{percent}")
        self.root.after(0, _update)
        

    def set_current_file_text(self, filename):
        """Şu an işlenen dosyanın adını büyük metinle günceller."""
        self.root.after(0, lambda: self.lbl_current_file.config(text=f"İşleniyor: {filename}"))

    def update_overall_progress(self, current_file_idx, total_files):
        """Genel dosya ilerleme çubuğunu günceller."""
        def _update():
            self.overall_progress_bar["maximum"] = total_files
            self.overall_progress_bar["value"] = current_file_idx
            self.lbl_overall_status.config(text=f"Genel İlerleme: {current_file_idx} / {total_files} Dosya Tamamlandı")
        self.root.after(0, _update)

    def set_gui_state(self, state):
        """İşlem sırasında butonları devre dışı bırakır/açar."""
        def _set():
            self.btn_start.config(state=state)
        self.root.after(0, _set)

    # --- JSON / Kimlik Doğrulama ---
    def check_saved_json(self):
        saved_key = self.config.get("key_path")
        if saved_key and os.path.exists(saved_key):
            self.lbl_json_status.config(text="Google JSON Anahtarı: Hazır", foreground="green")
            self.log("Kaydedilmiş JSON anahtarı bulundu.")

    def select_json_key(self):
        default_dir = self.config.get("last_key_dir", os.path.expanduser("~"))
        key_path = filedialog.askopenfilename(
            title="Google Service Account JSON Dosyasını Seç",
            initialdir=default_dir,
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")]
        )
        if key_path:
            self.config["key_path"] = key_path
            self.config["last_key_dir"] = os.path.dirname(key_path)
            save_config(self.config)
            self.lbl_json_status.config(text="Google JSON Anahtarı: Hazır", foreground="green")
            self.log(f"Yeni JSON anahtarı seçildi: {os.path.basename(key_path)}")
            
            # --- PATCH BURADA ---
            # JSON seçildikten hemen sonra dosya seçimini başlat:
            self.start_processing_thread()

    def authenticate(self):
        key_path = self.config.get("key_path")
        if not key_path or not os.path.exists(key_path):
            raise RuntimeError("Lütfen önce geçerli bir Google JSON Anahtarı seçin.")

        self.log("Vertex AI ile kimlik doğrulaması yapılıyor...")
        credentials = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        
        return genai.Client(
            vertexai=True,
            project=credentials.project_id,
            location=LOCATION,
            credentials=credentials, 
            http_options=types.HttpOptions(
                timeout=100000, 
                retry_options=types.HttpRetryOptions(
                    attempts=4, initial_delay=10.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504]
                )
            )
        )

    # --- İşlem Mantığı ---
    def start_processing_thread(self):
        # Dosya seçimini ana thread'de (UI'da) yap
        default_dir = self.config.get("last_input_dir", os.path.expanduser("~"))
        input_files = filedialog.askopenfilenames(
            title="Dosya(ları) Seç (.pdf, .txt, .srt)",
            initialdir=default_dir,
            filetypes=[
                ("Desteklenen Dosyalar", "*.pdf *.txt *.srt"), 
                ("PDF Dosyaları", "*.pdf"),
                ("Metin Dosyaları", "*.txt *.srt")
            ]
        )
        
        if not input_files:
            return

        self.config["last_input_dir"] = os.path.dirname(input_files[0])
        save_config(self.config)

        # UI'ı kilitle ve arka plan iş parçacığını başlat
        self.set_gui_state(tk.DISABLED)
        self.update_progress(0, 100)
        
        thread = threading.Thread(target=self.run_workflow, args=(input_files,))
        thread.daemon = True
        thread.start()

    def fix_block_with_gemini(self, target_block, context_blocks):
        context_text = "\n\n".join(context_blocks)
        prompt = f"""
CONTEXT BLOCKS (Includes surrounding subtitles for reference):
{context_text}

TARGET BLOCK TO FIX:
{target_block}
"""
        try:
            response = self.client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    system_instruction="""You are an expert Arabic linguist, Quranic scholar, and subtitle editor. 
                    Your task is to fix subtitle blocks containing Arabic text that have suffered from PDF/Word extraction errors. You must fix two main issues:
                    1. Broken, reversed Arabic text or fake ligatures.
                    2. Bidirectional (RTL/LTR) formatting mix-ups, where Arabic words get displaced from their correct position within Turkish sentences (e.g., leaving behind empty parentheses like `( )` or sitting awkwardly at the start/end of a line).

                    INSTRUCTIONS:
                    1. Reconstruct the correct Right-to-Left Arabic text or Quranic verse by using the provided Context Blocks.
                    2. If the Arabic text is displaced, move it back into its correct logical position within the Turkish sentence (for example, insert it back inside the empty parentheses).
                    3. Keep ALL timestamps, block numbers, and Turkish text intact.
                    4. DO NOT add conversational text, markdown formatting (like ```), or explain what verse it is. Output NOTHING but the fully corrected target block."""
                )
            )
            time.sleep(1) 
            return response.text.strip()
            
        except Exception as e:
            self.log(f"  [!] Bu blokta API Hatası: {e}")
            time.sleep(1) 
            return target_block

    def run_workflow(self, input_files):
        try:
            self.log("\n" + "="*50)
            self.log("İşlem Başlatıldı...")
            
            if not self.client:
                self.client = self.authenticate()
                self.log("Kimlik doğrulama başarılı.")

            total_files = len(input_files)
            self.update_overall_progress(0, total_files) # Genel bar sıfırlandı

            for file_idx, input_file in enumerate(input_files):
                filename = os.path.basename(input_file)
                self.set_current_file_text(filename) # Büyük metin güncellendi
                self.log(f"\n---> {filename} işleniyor...")
                
                output_file = os.path.splitext(input_file)[0] + "_duzeltilmiş.srt"
                content = ""

                # 1. PDF ise bellekte doğrudan String SRT'ye dönüştür
                if input_file.lower().endswith('.pdf'):
                    self.log("PDF belleğe okunuyor ve düzeltiliyor...")
                    content = convert_pdf_to_srt_string(input_file)
                    self.log("PDF'ten metin çıkarımı tamamlandı.")
                else:
                    self.log("Dosya okunuyor...")
                    content = read_file_safely(input_file)

                # 2. Blokları Ayrıştır
                blocks = re.split(r'\n\s*\n', content.strip())
                blocks = [b.strip() for b in blocks if b.strip()]
                total_blocks = len(blocks)
                
                arabic_pattern = re.compile(r'[\u0600-\u06FF]')
                fixed_blocks = []
                
                self.log(f"Toplam {total_blocks} altyazı bloğu bulundu. Gemini Arapça kelimeleri düzeltme işlemine başlıyor...")
                self.update_progress(0, total_blocks)

                # 3. Gemini ile Onarım Döngüsü
                for i, block in enumerate(blocks):
                    if arabic_pattern.search(block):
                        # self.log(f"Blok {i+1}/{total_blocks} onarılıyor (Arapça tespit edildi)...")
                        start_idx = max(0, i - 2)
                        end_idx = min(total_blocks, i + 3)
                        context_blocks = blocks[start_idx:end_idx]
                        
                        fixed_block = self.fix_block_with_gemini(block, context_blocks)
                        fixed_blocks.append(fixed_block)
                    else:
                        fixed_blocks.append(block)

                    # Arayüzdeki ilerleme çubuğunu güncelle
                    self.update_progress(i + 1, total_blocks)

                # 4. Son Düzenlemeler (Regex formatlama düzeltmeleri)
                final_content = "\n\n".join(fixed_blocks)
                final_content = final_content.replace('\r\n', '\n')
                final_content = re.sub(r'^(\d+)\s*\n+(\d{2}:\d{2}:\d{2},\d{3}\s*-->)', r'\1\n\2', final_content, flags=re.MULTILINE)
                final_content = re.sub(r'(\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3})\s*\n+', r'\1\n', final_content)
                final_content = re.sub(r'([^\n])\s*\n+(\d+)\n+(\d{2}:\d{2}:\d{2},\d{3}\s*-->)', r'\1\n\n\2\n\3', final_content)
                final_content = final_content.strip() + '\n'
                
                # 5. Dosyaya Kaydet
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(final_content)
                    
                self.log(f"Başarılı! Dosya kaydedildi:\n{output_file}")
                self.update_overall_progress(file_idx + 1, total_files)

            self.log("\nTüm işlemler tamamlandı!")
            self.update_progress(100, 100)
            messagebox.showinfo("Bitti", "Tüm dosyalar başarıyla işlendi!")

        except Exception as e:
            self.log(f"\n[!] Kritik Hata: {str(e)}")
            messagebox.showerror("Hata", f"Bir hata oluştu:\n{str(e)}")
            
        finally:
            self.set_gui_state(tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    
    # 1. Set the icon right after creating the window
    root.iconbitmap(resource_path("app_icon.ico"))
    
    # 2. Initialize your app contents
    app = SubtitleFixerApp(root)
    
    # 3. Start the application loop (must always be the absolute last line)
    root.mainloop()
