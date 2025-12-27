import streamlit as st
from google import genai
from google.genai import types
from PIL import Image
import os
import time
import re
import json
from datetime import datetime
from dotenv import load_dotenv

import socket
import qrcode
from io import BytesIO

# Google Sheets integration
import gspread
from streamlit_js_eval import streamlit_js_eval

# Load environment variables
load_dotenv()

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
        worksheet.update('A1:I1', [['日付', '時間', 'ユーザー', '料理名', 'エネルギー(kcal)', 'たんぱく質(g)', '塩分(g)', 'カリウム(mg)', 'リン(mg)']])
    return spreadsheet

def log_to_spreadsheet(gc, nickname, meal_name, nutrition_data):
    """解析結果をスプレッドシートに追記"""
    try:
        spreadsheet = get_or_create_spreadsheet(gc)
        worksheet = spreadsheet.sheet1
        
        now = datetime.now()
        row = [
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M:%S'),
            nickname,
            meal_name,
            nutrition_data.get('energy', '不明'),
            nutrition_data.get('protein', '不明'),
            nutrition_data.get('salt', '不明'),
            nutrition_data.get('potassium', '不明'),
            nutrition_data.get('phosphorus', '不明')
        ]
        worksheet.append_row(row)
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
    patterns = {
        'energy': r'エネルギー[：:]*\s*[\*\*]*\s*([\d,\.]+)',
        'protein': r'(?:タンパク質|たんぱく質)[：:]*\s*[\*\*]*\s*([\d,\.]+)',
        'salt': r'塩分[相当量]*[：:]*\s*[\*\*]*\s*([\d,\.～~\-]+)',
        'potassium': r'カリウム[：:]*\s*[\*\*]*\s*([\d,\.]+)',
        'phosphorus': r'リン[：:]*\s*[\*\*]*\s*([\d,\.]+)'
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match and match.group(1):
            nutrition[key] = match.group(1).replace(',', '')
        else:
            nutrition[key] = '不明'
    
    return nutrition
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

# Load nickname from browser's local storage
stored_nickname = streamlit_js_eval(js_expressions="localStorage.getItem('dialysis_app_nickname')", key="get_nickname")

# Initialize session state
if 'nickname' not in st.session_state:
    st.session_state.nickname = None
if 'show_nickname_form' not in st.session_state:
    st.session_state.show_nickname_form = False

# Set nickname from local storage if available
if stored_nickname and not st.session_state.nickname:
    st.session_state.nickname = stored_nickname

# Display nickname or input form
if st.session_state.nickname:
    col_nick1, col_nick2 = st.columns([3, 1])
    with col_nick1:
        st.markdown(f"👋 こんにちは、**{st.session_state.nickname}** さん")
    with col_nick2:
        if st.button("名前を変更", key="change_nickname"):
            st.session_state.show_nickname_form = True
            st.session_state.nickname = None
            st.rerun()
else:
    st.markdown("### 👤 ニックネームを設定してください")
    st.caption("解析結果を記録するために使用します（本名でなくてOK）")
    
    with st.form("nickname_form"):
        new_nickname = st.text_input("ニックネーム", placeholder="例: 田中さん")
        submitted = st.form_submit_button("設定")
        
        if submitted and new_nickname:
            st.session_state.nickname = new_nickname
            # Save to browser's local storage
            streamlit_js_eval(js_expressions=f"localStorage.setItem('dialysis_app_nickname', '{new_nickname}')", key="set_nickname")
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
                            
                            if log_to_spreadsheet(gc, st.session_state.nickname, meal_name, nutrition_data):
                                st.success("📊 結果をスプレッドシートに保存しました！")
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
