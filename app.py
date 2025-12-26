import streamlit as st
import google.generativeai as genai
from PIL import Image
import os
from dotenv import load_dotenv

import socket
import qrcode
from io import BytesIO

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

# Configure Gemini
try:
    genai.configure(api_key=api_key)
except Exception as e:
    st.error(f"APIキーの設定に失敗しました: {e}")
    st.stop()

# --- QR Code & UI ---
st.title("透析食スキャナー 🥗")
st.write("食事の写真をアップロードまたは撮影して、透析患者向けの栄養素（塩分、カリウム、リンなど）を解析します。")

# Helper to get local IP and generate QR
# Only show this in the sidebar to keep main view clean
with st.sidebar:
    st.subheader("スマホでアクセス")
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
input_method = st.radio("入力方法を選択:", ["カメラで撮影", "画像をアップロード"])

image = None

if input_method == "カメラで撮影":
    img_file_buffer = st.camera_input("食事を撮影")
    if img_file_buffer:
        image = Image.open(img_file_buffer)
else:
    uploaded_file = st.file_uploader("画像を選択", type=["jpg", "jpeg", "png"])
    if uploaded_file:
        image = Image.open(uploaded_file)

if image:
    st.image(image, caption="解析する画像", width='stretch')

    if st.button("栄養解析を開始"):
        with st.spinner("Geminiが解析中..."):
            try:
                # Construct Prompt
                prompt = """
                あなたは透析患者の食事管理を支援する専門の栄養士AIです。
                渡された食事の画像を解析し、以下の情報を日本語で出力してください。
                推定値で構いませんので、透析管理において重要な以下の項目を特に重視してください。

                出力フォーマット:
                ## 料理名: [推定される料理名]
                
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

                # Prepare the model
                # Try a list of models in order of preference
                candidate_models = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-1.5-pro']
                response = None
                last_error = None

                for model_name in candidate_models:
                    try:
                        st.info(f"モデル `{model_name}` で解析を試みています...")
                        model = genai.GenerativeModel(model_name)
                        response = model.generate_content([prompt, image])
                        break # Success, exit loop
                    except Exception as e:
                        last_error = e
                        continue
                
                if response:
                    st.success("解析完了！")
                    st.markdown(response.text)
                else:
                    st.error(f"すべてのモデルで解析に失敗しました。")
                    st.error(f"最後のエラー: {last_error}")
                    
                    # Connection check / List models hint
                    try:
                        st.write("---")
                        st.write("利用可能なモデル一覧:")
                        for m in genai.list_models():
                            if 'generateContent' in m.supported_generation_methods:
                                st.write(f"- {m.name}")
                    except Exception as list_err:
                        st.write(f"モデル一覧の取得にも失敗しました: {list_err}")

            except Exception as e:
                st.error(f"解析中にエラーが発生しました: {e}")
