import os
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import yfinance as yf
from FinMind.data import DataLoader

# ================= 策略與系統參數設定 =================
DAYS_WINDOW = 20        # 觀測天數 window (近 20 個交易日)
MIN_BUY_DAYS = 12       # 核心精選買超天數門檻
MAX_AMPLITUDE = 20.0    # 振幅門檻上限 (%)

# 備選觀察門檻
ALT_A_BUY_DAYS = 10     # 備選 A：買超 >= 10 天 (振幅 > 20%)
ALT_B_MIN_BUY = 7       # 備選 B：買超 7~11 天 (振幅 <= 20%)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

def get_pure_stock_info(dl):
    """取得全台股上市與上櫃「純普通個股」清單（排除 ETF、權證、特種股）"""
    print("🔍 [1/3] 正在過濾並取得全台股純普通股清單...")
    try:
        df = dl.taiwan_stock_info()
        if df is None or df.empty:
            print("❌ FinMind 取得個股清單失敗！")
            return []

        df_filtered = df[df['type'].isin(['twse', 'tpex'])].copy()
        
        # 排除非普通股類別 (ETF、權證、憑證等)
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

def process_single_stock(stock_id, dl, start_date):
    """
    【單檔處理核心】獨立抓取與計算單檔股票的 20 日振幅與買超天數
    """
    # 1. 抓取 K 線計算振幅 (yfinance)
    amplitude = None
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = f"{stock_id}{suffix}"
            df_k = yf.Ticker(ticker).history(period="1mo")
            if not df_k.empty and len(df_k) >= 5:
                hist = df_k.tail(DAYS_WINDOW)
                highest = hist['High'].max()
                lowest = hist['Low'].min()
                if lowest > 0 and not pd.isna(lowest) and not pd.isna(highest):
                    amplitude = round(float(((highest - lowest) / lowest) * 100), 2)
                    break
        except Exception:
            continue

    if amplitude is None:
        return None

    # 2. 抓取三大法人籌碼 (FinMind)
    try:
        df_chip = dl.taiwan_stock_institutional_investors_buy_sell(
            stock_id=stock_id,
            start_date=start_date
        )
        if df_chip is None or df_chip.empty:
            return None

        # 加總三大法人每日淨買賣超
        df_daily = df_chip.groupby('date')['buy_sell'].sum().reset_index()
        recent_20 = df_daily.sort_values('date').tail(DAYS_WINDOW)
        
        if len(recent_20) < 5:
            return None

        buy_days = int((recent_20['buy_sell'] > 0).sum())
        total_days = len(recent_20)

        return (stock_id, buy_days, total_days, amplitude)
    except Exception:
        return None

def scan_all_stocks_parallel(stock_list, dl):
    """
    【多執行緒併行掃描】平衡速度與 stability，約 3~6 分鐘跑完全台股
    """
    print("⚡ [2/3] 啟動多執行緒併行掃描全台股 K 線與法人籌碼...")
    start_date = (pd.Timestamp.now() - pd.Timedelta(days=40)).strftime('%Y-%m-%d')
    
    results = []
    # 使用 12 個 Worker 平行抓取，避免 API 頻率過高被封鎖
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(process_single_stock, stock_id, dl, start_date): stock_id 
            for stock_id in stock_list
        }
        
        completed_count = 0
        total = len(futures)
        for future in as_completed(futures):
            completed_count += 1
            if completed_count % 300 == 0 or completed_count == total:
                print(f"⏳ 掃描進度: {completed_count}/{total} ({round(completed_count/total*100, 1)}%)")
            
            res = future.result()
            if res is not None:
                results.append(res)

    print(f"✅ 完成 {len(results)} 檔個股之有效籌碼與振幅比對！")
    return results

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
    print("🚀 啟動台股全市場穩健多執行緒籌碼掃描引擎...")

    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)

    # 1. 取得純個股清單
    stock_list = get_pure_stock_info(dl)
    if not stock_list:
        print("❌ 無法取得股票清單，程式中斷。")
        return

    # 2. 多執行緒併行掃描
    scan_results = scan_all_stocks_parallel(stock_list, dl)

    perfect_matches = []   # 核心雙門檻 (買超>=12天, 振幅<=20%)
    high_buy_matches = []  # 備選 A (買超>=10天, 振幅>20%)
    low_amp_matches = []   # 備選 B (買超 7~11天, 振幅<=20%)

    # 3. 邏輯分類比對
    for stock_id, buy_days, total_days, amplitude in scan_results:
        is_buy_pass = buy_days >= MIN_BUY_DAYS       # >= 12 天
        is_amp_pass = 0 < amplitude <= MAX_AMPLITUDE  # <= 20%

        # 🎯 雙門檻精選
        if is_buy_pass and is_amp_pass:
            perfect_matches.append(f"🔥 [{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}%")
        
        # 👀 備選 A：買超 >= 10 天，但振幅偏高 (> 20%)
        elif buy_days >= ALT_A_BUY_DAYS and amplitude > MAX_AMPLITUDE:
            high_buy_matches.append(f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (籌碼集中，等待振幅收斂)")
            
        # 👀 備選 B：振幅 <= 20%，但買超接近達標 (7~11 天)
        elif is_amp_pass and ALT_B_MIN_BUY <= buy_days < MIN_BUY_DAYS:
            low_amp_matches.append(f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (低波動壓盤，法人升溫中)")

    # 4. 組裝郵件報告
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    subject = f"【全台股籌碼戰報】{today_str} - 雙門檻: {len(perfect_matches)} 檔 | 備選: {len(high_buy_matches)+len(low_amp_matches)} 檔"

    body = f"📊 全台股靜默籌碼收集掃描報告 ({today_str})\n"
    body += f"篩選標準：近 {DAYS_WINDOW} 交易日法人買超 ≥ {MIN_BUY_DAYS} 天，且價格振幅 ≤ {MAX_AMPLITUDE}%\n"
    body += f"掃描範圍：台股上市櫃純個股（完成 {len(scan_results)} 檔精準比對）\n"
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
    body += f"- 總比對個股: {len(scan_results)} 檔\n"
    body += f"- 雙門檻精選: {len(perfect_matches)} 檔\n"
    body += f"- 備選觀察總數: {len(high_buy_matches) + len(low_amp_matches)} 檔\n"
    body += f"- 腳本總耗時: {elapsed_time} 秒\n"
    body += "🤖 本郵件由 GitHub Actions 每日盤後自動掃描引擎發送。"

    send_email(subject, body)
    print(f"🎉 全部任務執行完畢！總耗時: {elapsed_time} 秒。")

if __name__ == "__main__":
    main()
