import os
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import yfinance as yf
import twstock  # 👈 1. 在最上方加入 import twstock
from FinMind.data import DataLoader

# ================= 策略與系統參數設定 =================
DAYS_WINDOW = 20        # 觀測天數 window (近 20 個交易日)
MIN_BUY_DAYS = 16       # 核心精選買超天數門檻16天
MAX_AMPLITUDE = 20.0    # 振幅門檻上限 (%)

# 備選觀察門檻
ALT_A_BUY_DAYS = 14     # 備選 A：買超 >= 14 天 (振幅 > 20%)
ALT_B_MIN_BUY = 11       # 備選 B：買超 11~13 天 (振幅 <= 20%)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

def get_pure_stock_info(dl):
    """取得全台股上市與上櫃「純普通個股」清單（排除 ETF、權證、特種股）"""
    print("🔍 [1/4] 正在過濾並取得全台股純普通股清單...")
    try:
        df = dl.taiwan_stock_info()
        if df is None or df.empty:
            print("❌ FinMind 取得個股清單失敗！")
            return []

        df_filtered = df[df['type'].isin(['twse', 'tpex'])].copy()
        
        # 排除非普通股類別
        exclude_categories = ['ETF', '存託憑證', '受益證券', '認購權證', '認售權證', '指數投資證券']
        if 'industry_category' in df_filtered.columns:
            df_filtered = df_filtered[~df_filtered['industry_category'].isin(exclude_categories)]
            
        # 僅保留 4 碼純數字代號
        stocks = df_filtered[df_filtered['stock_id'].str.contains(r'^\d{4}$', na=False)]['stock_id'].tolist()
        print(f"✅ 成功鎖定 {len(stocks)} 檔台股純個股！")
        return stocks
    except Exception as e:
        print(f"❌ 取得個股清單失敗，錯誤: {e}")
        return []

# 全局變數設定
DAYS_WINDOW = 20       # 籌碼計算視窗：近 20 個交易日 (買超 > 14 天)
AMP_DAYS_WINDOW = 40   # 振幅計算視窗：近 40 個交易日 (約 2 個月)

def calc_amplitude_single(stock_id):
    """單檔股票 2 個月 (40日) 振幅計算 (yfinance)"""
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = f"{stock_id}{suffix}"
            # 1. 抓取 2 個月歷史 K 線資料
            df_k = yf.Ticker(ticker).history(period="2mo")

            # 2. 確保有足夠的 K 線資料 (至少 20 筆以上)
            if not df_k.empty and len(df_k) >= 20:
                # 取近 40 個交易日
                hist = df_k.tail(DAYS_WINDOW)
                highest = hist['High'].max()
                lowest = hist['Low'].min()
                if lowest > 0 and not pd.isna(lowest) and not pd.isna(highest):
                    # 3. 計算 2 個月的高低差振幅 %
                    amp = round(float(((highest - lowest) / lowest) * 100), 2)
                    return (stock_id, amp)
        except Exception:
            continue
    return None

def fetch_chips_in_chunks(dl, target_stocks, chunk_size=50):
    """
    【分區打合包 API】每 50 檔打包為一次 API 請求，兼顧免費層級權限與 Rate Limit
    """
    print(f"🎯 [3/4] 第二階段：分批抓取籌碼 (鎖定 {len(target_stocks)} 檔潛力股，共分 {((len(target_stocks)-1)//chunk_size)+1} 批)...")
    start_date = (pd.Timestamp.now() - pd.Timedelta(days=40)).strftime('%Y-%m-%d')
    
    all_dfs = []
    
    # 將 target_stocks 切成每 chunk_size (50 檔) 一組
    for i in range(0, len(target_stocks), chunk_size):
        chunk = target_stocks[i:i + chunk_size]
        try:
            # FinMind 支援 stock_id 傳入單一字串或以逗號分隔的多個 ID，此處使用單檔/多檔迴圈保護機制
            # 傳入列表或逗號分隔字串
            stock_param = ",".join(chunk) if len(chunk) > 1 else chunk[0]
            
            df_chunk = dl.taiwan_stock_institutional_investors(
                stock_id=stock_param,
                start_date=start_date
            )
            
            if df_chunk is not None and not df_chunk.empty:
                all_dfs.append(df_chunk)
                
            time.sleep(0.5) # 輕量延遲避開 Rate Limit
        except Exception as e:
            print(f"⚠ 批次 {i//chunk_size + 1} 抓取受阻，降級為逐檔備援模式...")
            # 降級備援：若多檔請求被拒，改回分批小量單檔抓取
            for sid in chunk:
                try:
                    df_single = dl.taiwan_stock_institutional_investors(stock_id=sid, start_date=start_date)
                    if df_single is not None and not df_single.empty:
                        all_dfs.append(df_single)
                    time.sleep(0.2)
                except Exception:
                    continue

    if not all_dfs:
        print("⚠ 無法取得任何籌碼資料！")
        return {}

    df_chip = pd.concat(all_dfs, ignore_index=True)
    df_chip['stock_id'] = df_chip['stock_id'].astype(str)

    # 計算單日淨買賣超 (buy - sell)
    if 'buy_sell' in df_chip.columns:
        df_chip['net_buy'] = df_chip['buy_sell']
    elif 'buy' in df_chip.columns and 'sell' in df_chip.columns:
        df_chip['net_buy'] = df_chip['buy'] - df_chip['sell']
    else:
        print("⚠ 無法識別籌碼買賣超欄位！")
        return {}

    # 每日各法人淨買賣超加總
    df_daily = df_chip.groupby(['stock_id', 'date'])['net_buy'].sum().reset_index()

    chip_dict = {}
    for stock_id, group in df_daily.groupby('stock_id'):
        recent_20 = group.sort_values('date').tail(DAYS_WINDOW)
        if len(recent_20) >= 5:
            buy_days = int((recent_20['net_buy'] > 0).sum())
            chip_dict[stock_id] = (buy_days, len(recent_20))

    print(f"✅ 成功完成 {len(chip_dict)} 檔個股之籌碼比對！")
    return chip_dict

def send_email(subject, body):
    """發送 Gmail SMTP 戰報"""
    if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAIL:
        print("❌ 缺少 Email 設定變數，取消寄送。")
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("✉️ 每日籌碼戰報 Email 已成功寄出！")
    except Exception as e:
        print(f"❌ Email 發送失敗: {e}")

def main():
    start_time = time.time()
    print("🚀 啟動台股全市場兩階段流線型籌碼掃描引擎...")

    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)

    # 1. 取得個股清單
    stock_list = get_pure_stock_info(dl)
    if not stock_list:
        print("❌ 無法取得股票清單，程式中斷。")
        return

    # 2. 第一階段：併行計算全市場 20 日振幅 (yfinance)
    print("⚡ [2/4] 第一階段：極速掃描全市場 K 線與振幅 (yfinance)...")
    amp_dict = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(calc_amplitude_single, sid): sid for sid in stock_list}
        for future in as_completed(futures):
            res = future.result()
            if res is not None:
                sid, amp = res
                amp_dict[sid] = amp

    print(f"✅ 成功計算出 {len(amp_dict)} 檔有效 2 個月振幅！")

    # 3. 篩選出潛力池（振幅 <= 35.0% 進行籌碼比對）
    candidate_stocks = [sid for sid, amp in amp_dict.items() if 0 < amp <= 35.0]

    # 4. 第二階段：50 檔打包併行請求籌碼
    chip_dict = fetch_chips_in_chunks(dl, candidate_stocks, chunk_size=50)

    # 5. 邏輯交叉比對與分類
    perfect_matches = []   # 核心雙門檻 (買超>=12天, 振幅<=20%)
    high_buy_matches = []  # 備選 A (買超>=10天, 振幅>20%)
    low_amp_matches = []   # 備選 B (買超 7~11天, 振幅<=20%)

    scanned_count = len(chip_dict)

    for stock_id, (buy_days, total_days) in chip_dict.items():
        amplitude = amp_dict[stock_id]
        is_buy_pass = buy_days >= MIN_BUY_DAYS       # >= 12 天
        is_amp_pass = 0 < amplitude <= MAX_AMPLITUDE  # <= 20%

        # ---------------------------------------------------------
        # 👈 2. 加入這段安全的股票名稱查詢與組裝邏輯
        # ---------------------------------------------------------
        if is_buy_pass and is_amp_pass:
        # 安全的寫法：若 twstock 查不到名稱就回傳 None，絕不引發 KeyError
        stock_info = twstock.codes.get(stock_id)
        stock_name = stock_info.name if stock_info else ""

        # 有名稱就留空格接名稱，沒名稱就保持原樣
        name_display = f" {stock_name}" if stock_name else ""

        perfect_matches.append(
            f"🔥 [{stock_id}]{name_display} | 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}%"
        )
        # ---------------------------------------------------------

        # 🎯 雙門檻精選
        if is_buy_pass and is_amp_pass:
            perfect_matches.append(f"🔥 [{stock_id}] {stock_name} | 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}%")
        
        # 👀 備選 A：買超 >= 10 天，但振幅偏高 (20% ~ 35%)
        elif buy_days >= ALT_A_BUY_DAYS and amplitude > MAX_AMPLITUDE:
            high_buy_matches.append(f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (籌碼集中，等待振幅收斂)")
            
        # 👀 備選 B：振幅 <= 20%，但買超接近達標 (7~11 天)
        elif is_amp_pass and ALT_B_MIN_BUY <= buy_days < MIN_BUY_DAYS:
            low_amp_matches.append(f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (低波動壓盤，法人升溫中)")

    # 6. 組裝郵件報告
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    subject = f"【全台股籌碼戰報】{today_str} - 雙門檻: {len(perfect_matches)} 檔 | 備選: {len(high_buy_matches)+len(low_amp_matches)} 檔"

    body = f"📊 全台股靜默籌碼收集掃描報告 ({today_str})\n"
    body += f"篩選標準：近 {DAYS_WINDOW} 交易日法人買超 ≥ {MIN_BUY_DAYS} 天，且價格振幅 ≤ {MAX_AMPLITUDE}%\n"
    body += f"掃描範圍：台股上市櫃純個股（完成 {scanned_count} 檔精準比對）\n"
    body += "==================================================\n\n"

    if perfect_matches:
        body += f"🎯 🔥 今日符合「雙門檻」之核心精選標的 ({len(perfect_matches)} 檔)：\n\n"
        body += "\n".join(perfect_matches) + "\n\n"
    else:
        body += "🎯 🔥 今日符合「雙門檻」之核心精選標的：0 檔\n"
        body += "(今日全市場無個股同時符合雙門檻條件，市場可能處於高波動或籌碼發散期)\n\n"

    body += "--------------------------------------------------\n"
    body += "👀 備選觀察區（符合單一條件之潛力股）：\n\n"

    body += f"【類別 A：法人持續進場（買超 ≥ {ALT_A_BUY_DAYS}/{DAYS_WINDOW} 天），等待振幅收斂】({len(high_buy_matches)} 檔)\n"
    body += ("\n".join(high_buy_matches) + "\n\n") if high_buy_matches else "（無符合標的）\n\n"

    body += f"【類別 B：價格極度壓盤（振幅 ≤ {MAX_AMPLITUDE}%），買超蓄勢待發 ({ALT_B_MIN_BUY}~{MIN_BUY_DAYS-1}天)】({len(low_amp_matches)} 檔)\n"
    body += ("\n".join(low_amp_matches) + "\n\n") if low_amp_matches else "（無符合標的）\n\n"

    elapsed_time = round(time.time() - start_time, 1)
    body += "==================================================\n"
    body += f"📈 系統執行摘要：\n"
    body += f"- 總比對個股: {scanned_count} 檔\n"
    body += f"- 雙門檻精選: {len(perfect_matches)} 檔\n"
    body += f"- 備選觀察總數: {len(high_buy_matches) + len(low_amp_matches)} 檔\n"
    body += f"- 腳本總耗時: {elapsed_time} 秒\n"
    body += "🤖 本郵件由 GitHub Actions 每日盤後自動掃描引擎發送。"

    send_email(subject, body)
    print(f"🎉 全部任務執行完畢！總耗時: {elapsed_time} 秒。")

if __name__ == "__main__":
    main()
