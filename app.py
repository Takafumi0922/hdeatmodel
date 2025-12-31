import streamlit as st
from google import genai
from google.genai import types
from PIL import Image
import os
import time
import re
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import socket
import qrcode
from io import BytesIO

# Google Sheets integration
import gspread

import requests
import base64

# Load environment variables
load_dotenv(override=True)

# Google Drive Integration via GAS (Secrets or Env)
default_gas_url = "https://script.google.com/macros/s/AKfycbxA4FyvHrRwGS9zK6-0PQn4CpGVaJ4vdmXAtttt2jsq9gJG18UBE0MG_j4YM_c6GzdiUw/exec"
gas_url = st.secrets.get("GAS_SCRIPT_URL", os.getenv("GAS_SCRIPT_URL", default_gas_url))

# Page Config
# --- Password Protection ---
# Simple password check
def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["password"] == st.secrets.get("APP_PASSWORD", os.getenv("APP_PASSWORD")):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # don't store password
        else:
            st.session_state["password_correct"] = False

    # First run, show input
    if "password_correct" not in st.session_state:
        st.text_input(
            "パスワードを入力してください", type="password", on_change=password_entered, key="password"
        )
        return False
    
    # Password incorrect
    elif not st.session_state["password_correct"]:
        st.text_input(
            "パスワードを入力してください", type="password", on_change=password_entered, key="password"
        )
        st.error("😕 パスワードが違います")
        return False
    
    # Password correct
    else:
        return True

# Apply the password check
# Set APP_PASSWORD in .streamlit/secrets.toml (Cloud) or .env (Local)
# If no password is set in environment, skip check (Development convenience)
app_password = os.getenv("APP_PASSWORD")
if app_password: # Only check if password is set environment variable
    if not check_password():
        st.stop()

# --- API Key Management ---
# Try to get API key from environment
api_key = os.getenv("GOOGLE_API_KEY")

# Only show sidebar input if API key is NOT in environment (Developer mode fallback)
if not api_key:
    st.sidebar.warning("APIキーが設定されていません。")
    api_key = st.sidebar.text_input("Gemini API Key", type="password")

if api_key:
    api_key = api_key.strip()
else:
    st.error("APIキーが見つかりません。.envファイルを設定するか、サイドバーに入力してください。")
    st.stop()

# Configure Gemini Client (new SDK)
try:
    client = genai.Client(api_key=api_key)
except Exception as e:
    st.error(f"APIキーの設定に失敗しました: {e}")
    st.stop()

# --- PDF Reference ---
@st.cache_resource
def upload_reference_pdf():
    pdf_path = "食品成分表.pdf"
    if os.path.exists(pdf_path):
        try:
            # Upload the file to Gemini using new SDK
            with open(pdf_path, "rb") as f:
                uploaded_file = client.files.upload(file=f, config={"mime_type": "application/pdf"})
            return uploaded_file
        except Exception as e:
            st.warning(f"参照用PDFのアップロードに失敗しました (推定モードで動作します): {e}")
            return None
    return None

# Upload PDF once when app starts (cached)
pdf_reference = upload_reference_pdf()

# --- Google Sheets Integration ---
def get_gspread_client():
    """Googleスプレッドシートクライアントを取得"""
    try:
        # Streamlit Secretsからサービスアカウント認証情報を取得
        credentials_dict = st.secrets.get("gcp_service_account", None)
        if credentials_dict:
            gc = gspread.service_account_from_dict(dict(credentials_dict))
            return gc
    except Exception as e:
        st.warning(f"スプレッドシート連携が設定されていません: {e}")
    return None

def get_or_create_spreadsheet(gc, spreadsheet_name="栄養管理AI"):
    """スプレッドシートを取得または作成"""
    try:
        # 既存のスプレッドシートを開く
        spreadsheet = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        # 存在しない場合は作成
        spreadsheet = gc.create(spreadsheet_name)
        # ヘッダー行を追加
        worksheet = spreadsheet.sheet1
        worksheet.update('A1:K1', [['日付', '時間', 'ユーザー', '料理名', '食事写真', 'エネルギー(kcal)', 'たんぱく質(g)', '塩分(g)', 'カリウム(mg)', 'リン(mg)', '解析結果全文']])
    return spreadsheet

# --- Google Drive Integration via GAS ---
def upload_image_to_gas(image, filename):
    """画像をGAS経由でGoogle Driveにアップロード"""
    # 環境変数またはSecretsから取得（取得できない場合はハードコードされた値を使用）
    default_gas_url = "https://script.google.com/macros/s/AKfycbxA4FyvHrRwGS9zK6-0PQn4CpGVaJ4vdmXAtttt2jsq9gJG18UBE0MG_j4YM_c6GzdiUw/exec"
    gas_url = st.secrets.get("GAS_SCRIPT_URL", os.getenv("GAS_SCRIPT_URL", default_gas_url))
    
    if not gas_url:
        st.warning("⚠️ GAS_SCRIPT_URLが設定されていません。")
        return None

    try:
        # 画像をBase64文字列に変換
        img_byte_arr = BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        img_base64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
        
        payload = {
            'filename': filename,
            'image_data': img_base64,
            'folder_name': '食事写真' # GAS側でこのフォルダを探します
        }
        
        response = requests.post(gas_url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('status') == 'success':
                return result.get('url')
            else:
                st.warning(f"GASエラー: {result.get('message')}")
        else:
            st.warning(f"GAS通信エラー: {response.status_code}")
            
    except Exception as e:
        st.warning(f"画像アップロード処理でエラー: {e}")
    
    return None

def log_to_spreadsheet(gc, nickname, meal_name, nutrition_data, full_text="", image_url=""):
    """解析結果をスプレッドシートに追記"""
    try:
        spreadsheet = get_or_create_spreadsheet(gc)
        worksheet = spreadsheet.sheet1
        
        # 日本時間 (JST) を取得
        JST = timezone(timedelta(hours=9), 'JST')
        now = datetime.now(JST)
        
        # 画像URLがある場合はIMAGE関数として設定
        image_formula = f'=IMAGE("{image_url}")' if image_url else ""
        
        row = [
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M:%S'),
            nickname,
            meal_name,
            image_formula,
            nutrition_data.get('energy', '不明'),
            nutrition_data.get('protein', '不明'),
            nutrition_data.get('salt', '不明'),
            nutrition_data.get('potassium', '不明'),
            nutrition_data.get('phosphorus', '不明'),
            full_text
        ]
        
        # append_rowはデフォルトで数式を文字列として扱うので、
        # value_input_option='USER_ENTERED'を指定して数式として認識させる
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        return True
    except Exception as e:
        st.warning(f"スプレッドシートへの保存に失敗しました: {e}")
        return False

def parse_nutrition_from_response(response_text):
    """AI応答から栄養素を抽出"""
    nutrition = {}
    
    # 料理名を抽出
    meal_match = re.search(r'料理名[：:]\s*(.+)', response_text)
    if meal_match:
        nutrition['meal_name'] = meal_match.group(1).strip()
    else:
        nutrition['meal_name'] = '不明'
    
    # 各栄養素を抽出 (数値のみ)
    # より柔軟な正規表現に変更
    patterns = {
        'energy': r'エネルギー.*?([\d,\.～~\-]+)',
        'protein': r'(?:タンパク質|たんぱく質).*?([\d,\.～~\-]+)',
        'salt': r'塩分.*?([\d,\.～~\-]+)',
        'potassium': r'カリウム.*?([\d,\.～~\-]+)',
        'phosphorus': r'リン.*?([\d,\.～~\-]+)'
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match and match.group(1):
            val = match.group(1).replace(',', '').replace('～', '〜').replace('~', '〜')
            nutrition[key] = val
        else:
            nutrition[key] = '不明'
    
    return nutrition

# --- 管理者モード用関数 ---
def get_all_records(gc, spreadsheet_name="栄養管理AI"):
    """スプレッドシートから全データを取得"""
    try:
        spreadsheet = gc.open(spreadsheet_name)
        worksheet = spreadsheet.sheet1
        records = worksheet.get_all_records()
        return records
    except Exception as e:
        st.warning(f"データ取得に失敗しました: {e}")
        return []

def classify_meal_type(time_str):
    """時刻から食事区分を判定"""
    try:
        # HH:MM:SS 形式を想定
        parts = time_str.split(':')
        hour = int(parts[0])
        
        if 5 <= hour < 10:
            return "🌅 朝食"
        elif 10 <= hour < 15:
            return "☀️ 昼食"
        elif 15 <= hour < 22:
            return "🌙 夕食"
        else:
            return "🌃 夜食"
    except:
        return "❓ 不明"

def parse_nutrition_value(value):
    """栄養素の値を数値に変換（範囲の場合は中間値）"""
    try:
        if isinstance(value, (int, float)):
            return float(value)
        value_str = str(value).replace(',', '').replace(' ', '')
        # 範囲表記（〜、-、~）の場合は中間値を取る
        for sep in ['〜', '～', '~', '-']:
            if sep in value_str:
                parts = value_str.split(sep)
                nums = [float(p) for p in parts if p]
                return sum(nums) / len(nums)
        return float(value_str)
    except:
        return 0.0

# Custom CSS for styling
st.markdown("""
<style>
    .reportview-container {
        background: #f0f2f6;
    }
    .main-header {
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
        color: #333;
        text-align: center;
        padding: 2rem 0;
    }
    .stButton>button {
        width: 100%;
        background-color: #ff4b4b;
        color: white;
        border-radius: 10px;
        height: 3em;
        font-weight: bold;
    }
    .result-card {
        background-color: white;
        padding: 20px;
        border-radius: 15px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        margin-top: 20px;
    }
    .disclaimer {
        font-size: 0.8em;
        color: #666;
        margin-top: 30px;
        border-top: 1px solid #ddd;
        padding-top: 10px;
    }
</style>
""", unsafe_allow_html=True)

st.markdown("<h1 class='main-header'>透析 栄養管理AIアプリ 🥗Ver1.1</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center;'>食事の写真を撮るorアップロードするだけで、透析管理に必要な栄養素をAIが瞬時に解析します。</p>", unsafe_allow_html=True)

# Status indicator
if pdf_reference:
    st.markdown("✅ **食品成分表データロード済み**: 高精度モードで動作中")
else:
    st.caption("ℹ️ 標準モードで動作中 (成分表PDF未検出)")

# --- Nickname Section (with Local Storage) ---
st.markdown("---")

# Initialize gspread client
gc = get_gspread_client()
if gc:
    st.markdown("✅ **スプレッドシート連携**: 有効")
else:
    st.caption("ℹ️ スプレッドシート連携が未設定です（結果はローカル表示のみ）")

# --- Nickname Section (URLパラメータ方式) ---
# URLから nickname パラメータを取得
query_params = st.query_params
url_nickname = query_params.get("nickname", None)

# Initialize session state
if 'nickname' not in st.session_state:
    st.session_state.nickname = None

# URLパラメータからニックネームを設定
if url_nickname and not st.session_state.nickname:
    st.session_state.nickname = url_nickname

# Display nickname or input form
if st.session_state.nickname:
    col_nick1, col_nick2 = st.columns([3, 1])
    with col_nick1:
        st.markdown(f"👋 こんにちは、**{st.session_state.nickname}** さん")
    with col_nick2:
        if st.button("名前を変更", key="change_nickname"):
            st.session_state.nickname = None
            # URLパラメータをクリア
            st.query_params.clear()
            st.rerun()
else:
    st.markdown("### 👤 ニックネームを設定してください")
    st.caption("解析結果を記録するために使用します（本名でなくてOK）")
    st.caption("💡 設定後、表示されるURLをブックマークすると次回から自動ログインできます")
    
    with st.form("nickname_form"):
        new_nickname = st.text_input("ニックネーム", placeholder="例: 田中さん")
        submitted = st.form_submit_button("設定")
        
        if submitted and new_nickname:
            st.session_state.nickname = new_nickname
            # URLパラメータに追加（これでURLが更新される）
            st.query_params["nickname"] = new_nickname
            st.rerun()

# --- Nutritional Guidelines Section ---
st.markdown("---")
st.markdown("### 📊 透析患者の1日栄養摂取目安")

# Initialize session state for weight
if 'user_weight' not in st.session_state:
    st.session_state.user_weight = None

# Display guidelines in a nice format
col_guide1, col_guide2 = st.columns(2)

with col_guide1:
    st.markdown("""
    | 栄養素 | 目安値 |
    |--------|--------|
    | **エネルギー** | 30〜35 kcal/kg/日 |
    | **たんぱく質** | 0.9〜1.2 g/kg/日 |
    | **食塩** | 6g 未満 |
    """)

with col_guide2:
    st.markdown("""
    | 栄養素 | 目安値 |
    |--------|--------|
    | **カリウム** | 2000mg 未満 |
    | **リン** | たんぱく質(g) × 15 以下 |
    """)

# Weight calculator
if st.button("🧮 体重換算で個人目安を計算"):
    st.session_state.show_weight_form = True

if st.session_state.get('show_weight_form', False):
    with st.form("weight_form"):
        st.markdown("#### あなたの体重を入力してください")
        weight_input = st.number_input("体重 (kg)", min_value=20.0, max_value=200.0, value=60.0, step=0.5)
        submitted = st.form_submit_button("計算")
        
        if submitted:
            st.session_state.user_weight = weight_input
            st.session_state.show_weight_form = False
            st.rerun()

# Display personalized guidelines if weight is set
if st.session_state.user_weight:
    weight = st.session_state.user_weight
    
    # Calculate personalized values
    energy_min = weight * 30
    energy_max = weight * 35
    protein_min = weight * 0.9
    protein_max = weight * 1.2
    phosphorus_max = protein_max * 15
    
    st.success(f"👤 **あなたの体重 ({weight}kg) に基づく1日の目安**")
    
    st.markdown(f"""
    | 栄養素 | あなたの目安値 |
    |--------|---------------|
    | **エネルギー** | {energy_min:.0f} 〜 {energy_max:.0f} kcal |
    | **たんぱく質** | {protein_min:.1f} 〜 {protein_max:.1f} g |
    | **食塩** | 6g 未満 |
    | **カリウム** | 2000mg 未満 |
    | **リン** | {phosphorus_max:.0f}mg 以下 |
    """)
    
    if st.button("🔄 体重をリセット"):
        st.session_state.user_weight = None
        st.rerun()

st.markdown("---")

# Helper to get local IP and generate QR
# Only show this in the sidebar to keep main view clean
with st.sidebar:
    st.header("設定")
    st.subheader("スマホで利用")
    try:
        # Get local IP address
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        
        # Streamlit default port is 8501
        network_url = f"http://{local_ip}:8501"
        
        # Generate QR Code
        qr = qrcode.QRCode(box_size=10, border=4)
        qr.add_data(network_url)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to bytes for streamlit
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        st.image(buf.getvalue(), caption="スマホでスキャンして起動", width=200)
        st.write(f"URL: {network_url}")
        
    except Exception as e:
        st.write("QRコードの生成に失敗しました")
    
    # --- 管理者モード切替 ---
    st.markdown("---")
    st.subheader("📊 管理者機能")
    
    # セッションステートで管理者モードを管理
    if 'admin_mode' not in st.session_state:
        st.session_state.admin_mode = False
    
    if st.session_state.admin_mode:
        if st.button("🏠 通常モードに戻る", key="exit_admin"):
            st.session_state.admin_mode = False
            st.rerun()
    else:
        if st.button("📊 管理者モードを開く", key="enter_admin"):
            st.session_state.admin_mode = True
            st.rerun()

# --- メインコンテンツ分岐 ---
if st.session_state.get('admin_mode', False):
    # ========== 管理者モード ==========
    st.markdown("---")
    st.markdown("## 📊 食事記録レポート（管理者モード）")
    
    if not gc:
        st.error("⚠️ スプレッドシート連携が設定されていないため、管理者モードは使用できません。")
    else:
        # データ取得
        with st.spinner("データを読み込み中..."):
            all_records = get_all_records(gc)
        
        if not all_records:
            st.warning("📭 スプレッドシートにデータがまだありません。")
        else:
            # ユーザー一覧を取得
            users = list(set([r.get('ユーザー', '') for r in all_records if r.get('ユーザー')]))
            users.sort()
            
            # --- フィルタUI ---
            st.markdown("### 🔍 検索条件")
            col_filter1, col_filter2, col_filter3 = st.columns(3)
            
            with col_filter1:
                selected_user = st.selectbox("👤 ユーザー", ["全員"] + users)
            
            with col_filter2:
                # 日付範囲（デフォルトは過去30日）
                from datetime import date
                today = date.today()
                default_start = today - timedelta(days=30)
                start_date = st.date_input("📅 開始日", default_start)
            
            with col_filter3:
                end_date = st.date_input("📅 終了日", today)
            
            # --- データフィルタリング ---
            filtered_records = []
            for record in all_records:
                # 日付フィルタ
                try:
                    record_date_str = record.get('日付', '')
                    if record_date_str:
                        record_date = datetime.strptime(record_date_str, '%Y-%m-%d').date()
                        if not (start_date <= record_date <= end_date):
                            continue
                except:
                    continue
                
                # ユーザーフィルタ
                if selected_user != "全員":
                    if record.get('ユーザー') != selected_user:
                        continue
                
                # 食事区分を追加
                time_str = record.get('時間', '')
                record['食事区分'] = classify_meal_type(time_str)
                
                filtered_records.append(record)
            
            # 日付・時間でソート
            filtered_records.sort(key=lambda x: (x.get('日付', ''), x.get('時間', '')))
            
            st.markdown(f"**{len(filtered_records)}件** のデータが見つかりました")
            
            if filtered_records:
                # --- 期間サマリー ---
                st.markdown("### 📈 期間サマリー")
                
                # 栄養素の集計
                total_energy = sum(parse_nutrition_value(r.get('エネルギー(kcal)', 0)) for r in filtered_records)
                total_protein = sum(parse_nutrition_value(r.get('たんぱく質(g)', 0)) for r in filtered_records)
                total_salt = sum(parse_nutrition_value(r.get('塩分(g)', 0)) for r in filtered_records)
                total_potassium = sum(parse_nutrition_value(r.get('カリウム(mg)', 0)) for r in filtered_records)
                total_phosphorus = sum(parse_nutrition_value(r.get('リン(mg)', 0)) for r in filtered_records)
                
                meal_count = len(filtered_records)
                
                # 日数を計算
                unique_dates = set(r.get('日付') for r in filtered_records if r.get('日付'))
                day_count = len(unique_dates) if unique_dates else 1
                
                col_sum1, col_sum2, col_sum3 = st.columns(3)
                
                with col_sum1:
                    st.metric("総食事回数", f"{meal_count}回")
                    st.metric("記録日数", f"{day_count}日")
                
                with col_sum2:
                    st.metric("平均エネルギー/食", f"{total_energy/meal_count:.0f} kcal" if meal_count else "0 kcal")
                    st.metric("平均たんぱく質/食", f"{total_protein/meal_count:.1f} g" if meal_count else "0 g")
                    st.metric("平均塩分/食", f"{total_salt/meal_count:.1f} g" if meal_count else "0 g")
                
                with col_sum3:
                    st.metric("1日平均エネルギー", f"{total_energy/day_count:.0f} kcal" if day_count else "0 kcal")
                    st.metric("1日平均たんぱく質", f"{total_protein/day_count:.1f} g" if day_count else "0 g")
                    st.metric("1日平均塩分", f"{total_salt/day_count:.1f} g" if day_count else "0 g")
                
                # --- グラフ表示 ---
                st.markdown("### 📊 日ごとの推移")
                
                # 日ごとの集計
                daily_data = {}
                for record in filtered_records:
                    date_key = record.get('日付', '')
                    if date_key not in daily_data:
                        daily_data[date_key] = {'energy': 0, 'protein': 0, 'salt': 0}
                    daily_data[date_key]['energy'] += parse_nutrition_value(record.get('エネルギー(kcal)', 0))
                    daily_data[date_key]['protein'] += parse_nutrition_value(record.get('たんぱく質(g)', 0))
                    daily_data[date_key]['salt'] += parse_nutrition_value(record.get('塩分(g)', 0))
                
                if daily_data:
                    import pandas as pd
                    df = pd.DataFrame([
                        {'日付': k, 'エネルギー(kcal)': v['energy'], 'たんぱく質(g)': v['protein'], '塩分(g)': v['salt']}
                        for k, v in sorted(daily_data.items())
                    ])
                    
                    st.line_chart(df.set_index('日付'))
                
                # --- 食事記録一覧 ---
                st.markdown("### 🍽️ 食事記録一覧")
                
                for record in filtered_records:
                    with st.expander(f"{record.get('日付', '')} {record.get('食事区分', '')} - {record.get('料理名', '不明')}"):
                        col_img, col_info = st.columns([1, 2])
                        
                        with col_img:
                            # 画像表示（IMAGE関数からURLを抽出）
                            image_cell = record.get('食事写真', '')
                            if image_cell and '=IMAGE(' in str(image_cell):
                                # =IMAGE("URL") からURLを抽出
                                url_match = re.search(r'=IMAGE\("([^"]+)"\)', str(image_cell))
                                if url_match:
                                    st.image(url_match.group(1), width=150)
                            elif image_cell and image_cell.startswith('http'):
                                st.image(image_cell, width=150)
                            else:
                                st.caption("📷 画像なし")
                        
                        with col_info:
                            st.markdown(f"**ユーザー**: {record.get('ユーザー', '不明')}")
                            st.markdown(f"**時間**: {record.get('時間', '不明')}")
                            st.markdown(f"**エネルギー**: {record.get('エネルギー(kcal)', '不明')} kcal")
                            st.markdown(f"**たんぱく質**: {record.get('たんぱく質(g)', '不明')} g")
                            st.markdown(f"**塩分**: {record.get('塩分(g)', '不明')} g")
                            st.markdown(f"**カリウム**: {record.get('カリウム(mg)', '不明')} mg")
                            st.markdown(f"**リン**: {record.get('リン(mg)', '不明')} mg")
    
    # 管理者モードの場合は通常モードを表示しない
    st.stop()

# ========== 通常モード（食事入力） ==========
# Input Method
st.write("---")
input_method = st.radio("入力方法", ["カメラで撮影", "画像をアップロード"], horizontal=True, label_visibility="collapsed")

image = None

col1, col2 = st.columns([1, 2])

with col1:
    if input_method == "カメラで撮影":
        img_file_buffer = st.camera_input("食事を撮影")
        if img_file_buffer:
            try:
                image = Image.open(img_file_buffer)
            except Exception as e:
                st.error(f"画像の読み込みに失敗しました: {e}")
    else:
        uploaded_file = st.file_uploader("画像を選択", type=["jpg", "jpeg", "png"])
        if uploaded_file:
            try:
                image = Image.open(uploaded_file)
            except Exception as e:
                st.error(f"ファイルを開けませんでした。破損しているか、対応していない形式の可能性があります: {e}")

with col2:
    if image:
        st.image(image, caption="解析対象の画像", width='stretch', use_column_width=True)
        
        # st.write("") # Spacer
        if st.button("栄養解析を開始"):
            # Variables to store result outside status block
            response = None
            last_error = None
            model_name = 'gemini-2.5-flash'  # gemini-3-flash doesn't exist yet
            
            # Use st.status for a better progression UI
            with st.status("🚀 解析プロセス起動...", expanded=True) as status:
                try:
                    # Simulation of scanning
                    status.write("🔍 画像データをスキャン中...")
                    progress_bar = status.progress(0)
                    for i in range(100):
                        time.sleep(0.01) # fast scan effect
                        progress_bar.progress(i + 1)
                    
                    status.write("🧬 食材と栄養成分を特定中...")
                    
                    # Construct Prompt with Web Search instructions
                    prompt_text = """
                    あなたは透析患者の食事管理を支援する専門の栄養士AIです。
                    渡された食事の画像を解析し、以下の情報を日本語で出力してください。

                    【重要：情報ソースの優先順位】
                    1. **添付の「食品成分表」PDF**: 記述があれば最優先で使用してください。
                    2. **Google検索**: コンビニ商品、チェーン店メニューなど、PDFにない商品は積極的にWeb検索で栄養成分を探してください。
                    3. **推定**: 上記で見つからない場合は、あなたの知識に基づいて推定してください。

                    出力フォーマット:
                    ## 料理名: [推定される料理名]
                    (※参照元: 成分表PDF / Web検索 / 推定 のいずれかを記載)
                    
                    ## 推定栄養素 (1食あたり)
                    - **エネルギー**: [数値] kcal
                    - **タンパク質**: [数値] g
                    - **塩分相当量**: [数値] g
                    - **カリウム**: [数値] mg
                    - **リン**: [数値] mg
                    - **水分量**: [数値] ml (推定)

                    ## 透析患者へのアドバイス
                    [この食事における注意点や、透析患者が食べる際のアドバイスを簡潔に]
                    """
                    
                    # Prepare content list
                    contents = [prompt_text, image]
                    if pdf_reference:
                        contents.append(pdf_reference)

                    # Call the model with Google Search enabled
                    status.write(f"🤖 AIモデル ({model_name}) に接続中...")
                    status.write("🌐 Google検索を有効化...")
                    
                    # Generate content with Google Search tool using new SDK (non-streaming for stability)
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    )
                    
                    status.update(label="✅ 解析完了！", state="complete", expanded=False)
                    
                except Exception as e:
                    last_error = e
                    status.update(label="❌ エラー発生", state="error", expanded=False)
            
            # Display result OUTSIDE of st.status so it shows immediately
            if response:
                st.balloons()
                st.markdown('<div class="result-card">', unsafe_allow_html=True)
                
                try:
                    # Try to get text from response
                    result_text = None
                    
                    # Method 1: Direct text attribute
                    if hasattr(response, 'text') and response.text:
                        result_text = response.text
                    # Method 2: Access via candidates
                    elif hasattr(response, 'candidates') and response.candidates:
                        for candidate in response.candidates:
                            if hasattr(candidate, 'content') and candidate.content:
                                # partsがNoneでないことを確認
                                if hasattr(candidate.content, 'parts') and candidate.content.parts:
                                    for part in candidate.content.parts:
                                        if hasattr(part, 'text') and part.text:
                                            result_text = (result_text or "") + part.text
                    
                    # Method 3: Extract from grounding_metadata (new SDK with Google Search)
                    if not result_text and hasattr(response, 'candidates') and response.candidates:
                        candidate = response.candidates[0]
                        if hasattr(candidate, 'grounding_metadata') and candidate.grounding_metadata:
                            gm = candidate.grounding_metadata
                            if hasattr(gm, 'grounding_supports') and gm.grounding_supports:
                                # Collect all text segments
                                segments = []
                                for support in gm.grounding_supports:
                                    if hasattr(support, 'segment') and support.segment:
                                        if hasattr(support.segment, 'text') and support.segment.text:
                                            segments.append(support.segment.text)
                                if segments:
                                    result_text = "\n".join(segments)
                    
                    if result_text:
                        st.markdown(result_text)
                        
                        # --- Log to Google Spreadsheet ---
                        if gc and st.session_state.nickname:
                            nutrition_data = parse_nutrition_from_response(result_text)
                            meal_name = nutrition_data.get('meal_name', '不明')
                            
                            # Debug: Show parsed data
                            with st.expander("🔍 解析データデバッグ（開発用）", expanded=False):
                                st.write("抽出されたデータ:", nutrition_data)
                                st.write("解析テキスト全文:", result_text)
                            
                            # --- 画像をGoogle Driveにアップロード (GAS経由) ---
                            image_url = ""
                            
                            # ファイル名を生成（日時 + ユーザー名 + 料理名）
                            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                            safe_meal_name = re.sub(r'[\\/*?:"<>|]', '', meal_name)[:20]
                            filename = f"{timestamp}_{st.session_state.nickname}_{safe_meal_name}.jpg"
                            
                            with st.spinner("📸 画像をGoogle Driveに保存中..."):
                                image_url = upload_image_to_gas(image, filename)
                            
                            if image_url:
                                st.success("📸 食事写真をGoogle Driveに保存しました！")
                            
                            if log_to_spreadsheet(gc, st.session_state.nickname, meal_name, nutrition_data, full_text=result_text, image_url=image_url):
                                st.success("📊 結果をスプレッドシートに保存しました！（全文も記録しました）")
                            else:
                                st.info("📊 結果のスプレッドシート保存をスキップしました")
                        elif not st.session_state.nickname:
                            st.info("💡 ニックネームを設定すると、結果がスプレッドシートに保存されます")
                    else:
                        st.warning("AIからの応答がありませんでした。")
                        st.write("**デバッグ情報:**")
                        st.write(f"Response type: {type(response)}")
                        st.write(f"Response: {response}")
                        
                except Exception as display_err:
                    st.error(f"結果の表示中にエラーが発生しました: {display_err}")
                    st.write(f"**Response object:** {response}")
                
                st.markdown('</div>', unsafe_allow_html=True)
                
            elif last_error:
                st.error("⚠️ 解析に失敗しました")
                
                # Friendly Error Handling
                err_msg = str(last_error)
                if "429" in err_msg or "ResourceExhausted" in err_msg:
                    st.warning("短時間に多くのリクエストを送ったため、一時的に利用が制限されています。1〜2分待ってから再試行してください。")
                elif "404" in err_msg or "NotFound" in err_msg:
                    st.warning(f"モデル `{model_name}` が見つかりませんでした。APIキーが正しいか確認してください。")
                else:
                    st.error(f"エラー詳細: {last_error}")

# Disclaimer
st.markdown("""
<div class="disclaimer">
    <strong>【免責事項】</strong><br>
    本アプリによる解析結果はAIによる推定値であり、実際の栄養成分と異なる場合があります。<br>
    あくまで日々の目安としてご利用いただき、厳密な栄養管理については医師や管理栄養士の指導に従ってください。
</div>
""", unsafe_allow_html=True)
