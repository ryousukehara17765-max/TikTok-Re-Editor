import streamlit as st
import os
import tempfile
import base64
from dotenv import load_dotenv
from utils.text_normalize import normalize_for_timing, remove_punctuation_for_display
from utils.transcription import GladiaAPI
from utils.text_formatter import GeminiFormatter
from utils.voicevox import VoiceVoxAPI
from utils.video_generator_ffmpeg import VideoGeneratorFFmpeg

# 環境変数を読み込み
load_dotenv()


def calculate_line_timestamps(lines, words):
    """逐次テキストマッチングで各行のタイムスタンプを計算

    Gladiaの文字起こしテキストに対して各行を順にマッチングし、
    正確なタイムスタンプを取得する。完全一致できない場合は
    あいまいマッチングで最良位置を探索する。

    Args:
        lines: テキスト行のリスト
        words: Gladiaの単語タイムスタンプリスト [{"word": "...", "start": 0.0, "end": 0.5}, ...]

    Returns:
        list: [{"start": 0.0, "end": 1.5, "text": "テキスト"}, ...]
    """
    # 1. 文字単位のタイムラインを構築
    #    各単語の時間をその文字数で均等に分配
    char_times = []  # [(char, start, end), ...]
    for w in words:
        word_norm = normalize_for_timing(w['word'])
        if not word_norm:
            continue
        char_count = len(word_norm)
        word_duration = w['end'] - w['start']
        for ci, ch in enumerate(word_norm):
            ch_start = w['start'] + (word_duration * ci / char_count)
            ch_end = w['start'] + (word_duration * (ci + 1) / char_count)
            char_times.append((ch, ch_start, ch_end))

    total_gladia_chars = len(char_times)

    # ユーザーテキストの正規化
    line_norms = [normalize_for_timing(line) for line in lines]
    total_user_chars = sum(len(ln) for ln in line_norms)

    print(f"[TIMING] User chars: {total_user_chars}, Gladia chars: {total_gladia_chars}, Diff: {total_user_chars - total_gladia_chars}")

    # フォールバック: どちらかが0の場合は均等分割
    if total_gladia_chars == 0 or total_user_chars == 0:
        total_duration = words[-1]['end'] if words else 1
        segment_duration = total_duration / max(len(lines), 1)
        return [{"start": i * segment_duration, "end": (i + 1) * segment_duration, "text": line}
                for i, line in enumerate(lines)]

    # 2. Gladiaテキスト全文を構築
    gladia_text = ''.join(ch for ch, _, _ in char_times)

    # 3. 逐次マッチング: 各行をGladiaテキスト内で順に探索
    search_pos = 0
    cumulative_user_chars = 0
    segments = []

    for line_idx, line in enumerate(lines):
        line_norm = line_norms[line_idx]
        if not line_norm:
            continue
        line_len = len(line_norm)

        # 探索範囲: search_posから前方を探索（比例位置もカバー）
        expected_pos = round((cumulative_user_chars / total_user_chars) * total_gladia_chars)
        scan_end = min(max(expected_pos + line_len * 2, search_pos + line_len * 5), total_gladia_chars)

        # 完全一致を試みる（search_posから前方探索）
        match_pos = gladia_text.find(line_norm, search_pos, scan_end)

        if match_pos >= 0:
            pos = match_pos
        else:
            # あいまいマッチ: search_posからスライディングウィンドウ探索
            best_pos = search_pos
            best_score = -1
            fuzzy_end = min(scan_end, total_gladia_chars - line_len + 1)
            for p in range(search_pos, max(search_pos + 1, fuzzy_end)):
                score = sum(1 for a, b in zip(line_norm, gladia_text[p:p + line_len]) if a == b)
                if score > best_score:
                    best_score = score
                    best_pos = p
            # マッチ品質が低い場合は比例位置にフォールバック
            if best_score < line_len * 0.3:
                pos = min(max(expected_pos, search_pos), total_gladia_chars - 1)
            else:
                pos = best_pos

        # タイムスタンプ取得
        pos = min(pos, total_gladia_chars - 1)
        end_pos = min(pos + line_len - 1, total_gladia_chars - 1)

        start_time = char_times[pos][1]
        end_time = char_times[end_pos][2]

        print(f"[TIMING] Line {line_idx}: pos={pos}, '{line_norm[:15]}' -> {start_time:.3f}s-{end_time:.3f}s")

        search_pos = end_pos + 1
        cumulative_user_chars += line_len

        segments.append({
            "start": start_time,
            "end": end_time,
            "text": line
        })

    return segments


def validate_segments(segments):
    """セグメントの順序・duration を検証してログ出力

    Returns:
        bool: 全てのバリデーションに通ったらTrue
    """
    valid = True
    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        if duration <= 0:
            print(f"[TIMING] WARNING: segment {i} has non-positive duration ({duration:.4f}s): {seg['text'][:20]}")
            valid = False
        if i > 0 and seg["start"] < segments[i - 1]["start"]:
            print(f"[TIMING] WARNING: segment {i} starts before segment {i-1} ({seg['start']:.4f} < {segments[i-1]['start']:.4f})")
            valid = False
        print(f"[TIMING] segment {i}: {seg['start']:.4f}s - {seg['end']:.4f}s ({duration:.4f}s) | {seg['text'][:30]}")
    if valid:
        print(f"[TIMING] All {len(segments)} segments validated OK")
    return valid


MAX_DISPLAY_CHARS = 14

# 分割候補: 助詞・接続助詞・動詞終止・括弧閉じの後ろで区切る
_SPLIT_AFTER = set('をにはがでともへてる】」』）')
# 強い境界: トピック(は)・括弧閉じは中央バランスより優先
_STRONG_SPLIT = set('は】」』）')
_MIN_SPLIT_SEGMENT = 4


def split_long_lines(text, max_display_chars=MAX_DISPLAY_CHARS):
    """表示文字数が上限を超える行を文節境界で自動分割"""
    result_lines = []
    for line in text.split('\n'):
        result_lines.extend(_split_line(line, max_display_chars))
    return '\n'.join(result_lines)


def _split_line(line, max_display_chars):
    """1行を自然な位置で分割（再帰）"""
    display_len = len(remove_punctuation_for_display(line))
    if display_len <= max_display_chars:
        return [line]

    # 分割候補を収集
    candidates = []
    display_count = 0
    for i, ch in enumerate(line):
        if remove_punctuation_for_display(ch) != '':
            display_count += 1
        is_split = ch in _SPLIT_AFTER
        # 「ない」パターン: な の直後の い も分割候補
        if not is_split and ch == 'い' and i > 0 and line[i - 1] == 'な':
            is_split = True
        if is_split and _MIN_SPLIT_SEGMENT <= display_count <= max_display_chars:
            remaining = display_len - display_count
            candidates.append((i + 1, display_count, remaining, ch in _STRONG_SPLIT))

    if candidates:
        # 優先1: 強い境界（は・】等）で最も後ろ（第2部分が5文字以上）
        strong = [c for c in candidates if c[3] and c[2] >= 5]
        if strong:
            best = max(strong, key=lambda c: c[1])
        else:
            # 優先2: 全候補から中央に最も近い（同距離なら大きい方優先）
            target = display_len / 2
            best = min(candidates, key=lambda c: (abs(c[1] - target), -c[1]))
        split_idx = best[0]
    else:
        # フォールバック：上限で強制分割（句読点は前行に残す）
        display_count = 0
        split_idx = len(line)
        for i, ch in enumerate(line):
            is_display = (remove_punctuation_for_display(ch) != '')
            if is_display and display_count >= max_display_chars:
                split_idx = i
                break
            if is_display:
                display_count += 1

    first = line[:split_idx]
    rest = line[split_idx:]
    if not rest or not rest.strip():
        return [line]
    # 分割された先頭部分に句読点がなければ「、」を追加
    _PUNCT_ENDS = ('、', '。', '！', '？', '!', '?', '．', '，')
    if not first.endswith(_PUNCT_ENDS):
        first = first + '、'
    return [first] + _split_line(rest, max_display_chars)


def check_line_overflow(text, max_display_chars=MAX_DISPLAY_CHARS):
    """はみ出す行のリストを返す [(行番号, 表示文字数, 行テキスト), ...]"""
    overflows = []
    for i, line in enumerate(text.split('\n'), 1):
        display_count = len(remove_punctuation_for_display(line))
        if display_count > max_display_chars:
            overflows.append((i, display_count, line))
    return overflows


# ページ設定
st.set_page_config(
    page_title="TikTok Re-Editor v3",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ========================================
# 認証チェック（Google OAuth + Lark Base）
# ========================================
from auth import check_auth, is_current_user_admin, get_current_user
from admin import render_admin_panel

# 管理者パネル表示フラグ
if "show_admin_panel" not in st.session_state:
    st.session_state.show_admin_panel = False

# 認証チェック（未承認の場合はここでstopされる）
if not check_auth():
    st.stop()

# 管理者パネル表示
if st.session_state.show_admin_panel and is_current_user_admin():
    render_admin_panel()
    if st.button("← アプリに戻る", key="back_to_app_btn"):
        st.session_state.show_admin_panel = False
        st.rerun()
    st.stop()

# 翻訳を無効化
st.markdown('<meta name="google" content="notranslate">', unsafe_allow_html=True)

# カスタムCSS - TikTokスタイルのボタンとUI
st.markdown("""
<style>
    /* TikTokカラー: シアン #00f2ea, ピンク #fe2c55, 黒背景 */

    /* ダークテーマの背景 */
    .stApp {
        background: #000000;
        color: #ffffff;
    }

    /* ヘッダースタイル */
    h1 {
        color: #ffffff !important;
        text-shadow:
            2px 2px 0px #fe2c55,
            -2px -2px 0px #00f2ea;
        font-weight: bold !important;
    }

    h2, h3 {
        color: #ffffff !important;
        text-shadow: 0 0 10px rgba(0, 242, 234, 0.5);
    }

    /* サイドバーを非表示 */
    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
        display: none !important;
    }

    /* 本文の左右余白を均等に */
    .block-container {
        padding: 2rem 3rem 2rem 3rem !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
        overflow-x: hidden !important;
    }

    .stApp {
        overflow-x: hidden !important;
    }

    /* expanderのスタイル - コンパクトに */
    [data-testid="stExpander"] {
        background: #00f2ea !important;
        border: none !important;
        border-radius: 8px !important;
        margin-bottom: 20px !important;
        width: fit-content !important;
    }
    [data-testid="stExpander"] summary {
        color: #000000 !important;
        font-weight: bold !important;
        padding: 8px 16px !important;
    }
    [data-testid="stExpander"] summary:hover {
        background: #00d4d4 !important;
        border-radius: 8px !important;
    }
    [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
        background: #1a1a1a !important;
        border: 1px solid #00f2ea !important;
        border-radius: 8px !important;
        padding: 15px !important;
        margin-top: 10px !important;
    }
    [data-testid="stExpander"] [data-testid="stExpanderDetails"] label {
        color: #ffffff !important;
    }
    [data-testid="stExpander"] [data-testid="stExpanderDetails"] a {
        color: #00f2ea !important;
    }


    /* 全てのボタンを左寄せ・同じ大きさに統一（BROWSE FILES除く） */
    .stButton > button,
    .stButton button,
    .stDownloadButton > button,
    .stDownloadButton button,
    button[kind="primary"] {
        background: #000000 !important;
        color: white !important;
        border: 2px solid #00f2ea !important;
        border-radius: 10px !important;
        padding: 12px 30px !important;
        font-size: 14px !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 2px !important;
        box-shadow: 0 0 15px rgba(0, 242, 234, 0.5) !important;
        transition: all 0.3s ease !important;
        width: auto !important;
        max-width: 100% !important;
        min-height: 45px !important;
        height: 45px !important;
        line-height: 1.2 !important;
        margin-right: auto !important;
        margin-left: 0 !important;
        display: block !important;
    }

    .stButton > button:hover:not(:disabled),
    .stButton button:hover:not(:disabled),
    .stDownloadButton > button:hover,
    .stDownloadButton button:hover,
    button[kind="primary"]:hover {
        background: #1a1a1a !important;
        border: 3px solid #00f2ea !important;
        color: #00f2ea !important;
        box-shadow:
            0 0 40px rgba(0, 242, 234, 1),
            0 0 60px rgba(0, 242, 234, 0.6),
            inset 0 0 20px rgba(0, 242, 234, 0.2) !important;
        transform: translateY(-3px) scale(1.02) !important;
    }

    /* テキストエリア */
    .stTextArea textarea {
        background: rgba(10, 10, 10, 0.9) !important;
        color: #ffffff !important;
        border: 2px solid rgba(0, 242, 234, 0.5) !important;
        border-radius: 8px !important;
        box-shadow: 0 0 15px rgba(0, 242, 234, 0.3) !important;
        caret-color: #00f2ea !important;
        padding: 10px !important;
        font-size: 14px !important;
        line-height: 1.6 !important;
    }

    /* テキストインプット */
    .stTextInput input {
        background: rgba(10, 10, 10, 0.9) !important;
        color: #ffffff !important;
        border: 2px solid rgba(0, 242, 234, 0.5) !important;
        border-radius: 8px !important;
        box-shadow: 0 0 15px rgba(0, 242, 234, 0.3) !important;
        caret-color: #00f2ea !important;
        padding: 8px 12px !important;
        font-size: 14px !important;
    }

    /* セレクトボックス */
    .stSelectbox > div > div {
        background: rgba(10, 10, 10, 0.9) !important;
        color: #ffffff !important;
        border: 2px solid rgba(0, 242, 234, 0.5) !important;
        border-radius: 10px !important;
    }

    /* スライダー */
    .stSlider > div > div > div {
        background: linear-gradient(90deg, #00f2ea 0%, #fe2c55 100%) !important;
    }

    /* 各種ラベルを白文字に */
    .stFileUploader label,
    [data-testid="stFileUploader"] label,
    .stFileUploader p,
    [data-testid="stFileUploader"] p,
    .stTextArea label,
    .stTextInput label,
    .stSelectbox label,
    .stSlider label {
        color: #ffffff !important;
    }

    /* インフォボックス */
    .stInfo {
        background: rgba(0, 242, 234, 0.1) !important;
        border: 2px solid rgba(0, 242, 234, 0.5) !important;
        border-radius: 10px !important;
        box-shadow: 0 0 15px rgba(0, 242, 234, 0.3) !important;
        color: #ffffff !important;
    }

    /* ファイルアップローダー */
    .stFileUploader {
        background: rgba(10, 10, 10, 0.9) !important;
        border: 2px solid rgba(0, 242, 234, 0.5) !important;
        border-radius: 10px !important;
        padding: 20px !important;
    }

    /* オーディオプレイヤー */
    audio {
        width: 100% !important;
        filter:
            drop-shadow(0 0 10px rgba(0, 242, 234, 0.5))
            drop-shadow(0 0 20px rgba(254, 44, 85, 0.3));
    }

    /* iPhone 15風フレーム */
    .iphone-frame {
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 40px 0;
    }

    .iphone-device {
        width: 240px;
        background: #1c1c1e;
        border-radius: 50px;
        padding: 12px;
        box-shadow:
            inset 0 0 0 3px #2c2c2e,
            inset 0 0 0 4px #1c1c1e,
            0 0 0 2px #0a0a0a,
            0 40px 80px rgba(0, 0, 0, 0.8),
            0 0 60px rgba(0, 242, 234, 0.1);
        position: relative;
    }

    /* サイドボタン */
    .iphone-device::before {
        content: "";
        position: absolute;
        right: -3px;
        top: 120px;
        width: 4px;
        height: 60px;
        background: #2c2c2e;
        border-radius: 0 2px 2px 0;
    }

    .iphone-device::after {
        content: "";
        position: absolute;
        left: -3px;
        top: 100px;
        width: 4px;
        height: 30px;
        background: #2c2c2e;
        border-radius: 2px 0 0 2px;
        box-shadow: 0 50px 0 #2c2c2e, 0 90px 0 #2c2c2e;
    }

    /* Dynamic Island */
    .iphone-dynamic-island {
        width: 100px;
        height: 32px;
        background: #000;
        border-radius: 20px;
        margin: 0 auto 8px auto;
        position: relative;
        z-index: 10;
        box-shadow: inset 0 0 4px rgba(255,255,255,0.1);
    }

    .iphone-screen {
        background: #000;
        border-radius: 42px;
        overflow: hidden;
        position: relative;
        border: 1px solid #333;
    }

    .iphone-screen video {
        width: 100% !important;
        height: auto !important;
        max-height: 450px !important;
        display: block !important;
    }

    /* ホームインジケーター */
    .iphone-home-indicator {
        width: 130px;
        height: 5px;
        background: #fff;
        border-radius: 3px;
        margin: 10px auto 5px auto;
        opacity: 0.8;
    }

    /* タブスタイル */
    .stTabs [data-baseweb="tab-list"] {
        gap: 15px;
        background: transparent !important;
        padding: 15px 10px 20px 10px;
        border: none !important;
        display: flex !important;
        flex-direction: row !important;
    }

    .stTabs [data-baseweb="tab"] {
        flex: 1 !important;
        width: 100% !important;
        height: 45px !important;
        padding: 12px 30px !important;
        background: #000000 !important;
        border: 2px solid #00f2ea !important;
        border-radius: 10px !important;
        color: white !important;
        font-size: 14px !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 2px !important;
        box-shadow: 0 0 15px rgba(0, 242, 234, 0.5) !important;
        transition: all 0.25s ease !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }

    .stTabs [data-baseweb="tab"]:hover {
        background: #1a1a1a !important;
        border: 3px solid #00f2ea !important;
        color: #00f2ea !important;
        box-shadow: 0 0 40px rgba(0, 242, 234, 1) !important;
        transform: translateY(-3px) scale(1.02) !important;
    }

    /* サクセスボックス - ピンク系 */
    .stSuccess {
        background: rgba(254, 44, 85, 0.1) !important;
        border: 2px solid rgba(254, 44, 85, 0.5) !important;
        border-radius: 10px !important;
        color: #ffffff !important;
    }

    /* 処理中の画面暗転を無効化 */
    [data-stale="true"],
    .stTabs [data-stale="true"],
    .stTabs[data-stale="true"],
    [data-testid="stTabs"][data-stale="true"],
    .stTabs,
    .stTabs [data-baseweb="tab-list"],
    .stTabs [data-baseweb="tab"] {
        opacity: 1 !important;
    }
</style>
""", unsafe_allow_html=True)

# セッションステートの初期化
if 'transcribed_text' not in st.session_state:
    st.session_state.transcribed_text = None
if 'formatted_text' not in st.session_state:
    st.session_state.formatted_text = None
if 'filename' not in st.session_state:
    st.session_state.filename = None
if 'generated_audio' not in st.session_state:
    st.session_state.generated_audio = None
if 'sample_audio' not in st.session_state:
    st.session_state.sample_audio = None
if 'generated_sns_content' not in st.session_state:
    st.session_state.generated_sns_content = None
if 'generated_video' not in st.session_state:
    st.session_state.generated_video = None
if 'preview_video' not in st.session_state:
    st.session_state.preview_video = None
if 'speaker_id' not in st.session_state:
    st.session_state.speaker_id = None
if 'speed' not in st.session_state:
    st.session_state.speed = 1.0
if 'pause_length' not in st.session_state:
    st.session_state.pause_length = 1.0
if 'audio_text' not in st.session_state:
    st.session_state.audio_text = None
if 'rephrased_result' not in st.session_state:
    st.session_state.rephrased_result = None
if 'hiragana_text' not in st.session_state:
    st.session_state.hiragana_text = None
if 'audio_segments' not in st.session_state:
    st.session_state.audio_segments = None
if 'audio_upload_mode' not in st.session_state:
    st.session_state.audio_upload_mode = False
if 'audio_file_path' not in st.session_state:
    st.session_state.audio_file_path = None
if 'audio_file_data' not in st.session_state:
    st.session_state.audio_file_data = None
if 'audio_words' not in st.session_state:
    st.session_state.audio_words = []
if 'edited_segments' not in st.session_state:
    st.session_state.edited_segments = None
if 'timestamped_segments' not in st.session_state:
    st.session_state.timestamped_segments = None
if 'gladia_words' not in st.session_state:
    st.session_state.gladia_words = []
if 'audio_upload_sns_content' not in st.session_state:
    st.session_state.audio_upload_sns_content = None

# タイトルとユーザーメニューを横並び
header_col1, header_col2 = st.columns([4, 1])
with header_col1:
    st.markdown('<h1 translate="no">TikTok Re-Editor v3</h1>', unsafe_allow_html=True)
with header_col2:
    st.markdown("<div style='height: 30px'></div>", unsafe_allow_html=True)
    user = get_current_user()
    if user:
        is_admin = is_current_user_admin()
        admin_badge = " 👑" if is_admin else ""
        with st.popover(f"👤 {user['nickname']}{admin_badge}"):
            st.markdown(f"**{user['email']}**")
            st.markdown(f"ログイン: {user['login_count']}回")
            if is_admin:
                if st.button("🔧 管理者パネル", key="header_admin_btn", use_container_width=True):
                    st.session_state.show_admin_panel = True
                    st.rerun()
            if st.button("🚪 ログアウト", key="header_logout_btn", use_container_width=True):
                st.logout()

st.markdown("文字起こし → 整形 → 音声アップロード → **透過動画生成**")

# フォント情報表示
_font_info = VideoGeneratorFFmpeg.get_current_font_info()
st.caption(f"🔤 使用フォント: **{_font_info['name']}**（{_font_info['size']}px）")

# ===========================================
# APIキー設定（最初に入力）
# ===========================================
st.header("APIキー設定")
st.info("💡 **APIキーはメモ帳等に保存しておくと便利です**（ブラウザを閉じると消えます）")

col1, col2 = st.columns(2)
with col1:
    gladia_api_key = st.text_input(
        "Gladia API Key（文字起こし用）",
        type="password",
        key="gladia_input",
        placeholder="Gladia APIキーを入力"
    )
    st.markdown('<a href="https://www.gladia.io/" target="_blank" style="color: #00f2ea; font-size: 12px;">🔗 Gladia APIキーを取得（無料）</a>', unsafe_allow_html=True)

with col2:
    gemini_api_key = st.text_input(
        "Gemini API Key（テキスト整形用）",
        type="password",
        key="gemini_input",
        placeholder="Gemini APIキーを入力"
    )
    st.markdown('<a href="https://aistudio.google.com/apikey" target="_blank" style="color: #00f2ea; font-size: 12px;">🔗 Gemini APIキーを取得（無料）</a>', unsafe_allow_html=True)

st.markdown("---")

# VOICEVOX URLはデフォルト値を使用（UIから削除）
voicevox_url = "http://localhost:50021"

# APIクライアントの初期化
gladia = GladiaAPI(gladia_api_key) if gladia_api_key else None
gemini = GeminiFormatter(gemini_api_key) if gemini_api_key else None
voicevox = VoiceVoxAPI(voicevox_url)

# ===========================================
# セクション1: 入力ソース選択
# ===========================================
# リセットボタン（APIキーは保持）
_sec1_col1, _sec1_col2 = st.columns([4, 1])
with _sec1_col1:
    st.header("1. 入力ソース選択")
with _sec1_col2:
    st.markdown("<div style='height: 20px'></div>", unsafe_allow_html=True)
    if st.button("NEW", key="reset_btn"):
        # APIキーとシステム系のキーを保持
        preserve_keys = {'gladia_input', 'gemini_input', 'show_admin_panel'}
        preserved = {k: st.session_state[k] for k in preserve_keys if k in st.session_state}
        # ファイルアップローダーのカウンターをインクリメント（ドラッグ&ドロップファイルをクリア）
        upload_counter = st.session_state.get('upload_counter', 0) + 1
        # 全セッションステートをクリア
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        # 保持するキーを復元
        for k, v in preserved.items():
            st.session_state[k] = v
        st.session_state['upload_counter'] = upload_counter
        st.rerun()

tab1, tab2, tab3, tab4 = st.tabs(["動画から生成", "ファイルから生成", "テキスト入力", "🎵 音声アップロード"])

with tab1:
    st.subheader("動画アップロード")

    _uc = st.session_state.get('upload_counter', 0)
    uploaded_file = st.file_uploader(
        "動画ファイルを選択してください",
        type=["mp4", "mov", "avi", "mkv", "webm"],
        key=f"video_uploader_{_uc}"
    )

    if uploaded_file is not None:
        # ファイルポインタを先頭にリセットしてから読み込む
        uploaded_file.seek(0)
        file_data = uploaded_file.read()

        # 元のファイル拡張子を維持
        import os
        file_ext = os.path.splitext(uploaded_file.name)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            tmp_file.write(file_data)
            tmp_file_path = tmp_file.name

        st.info(f"アップロードされたファイル: {uploaded_file.name}")

        if st.button("START", key="transcribe_btn"):
            if not gladia_api_key or not gemini_api_key:
                st.error("サイドバーでGladia APIキーとGemini APIキーを入力してください")
                st.stop()

            try:
                progress_bar = st.progress(0)

                progress_bar.progress(10)
                audio_url = gladia.upload_file(tmp_file_path)

                if audio_url:
                    progress_bar.progress(30)
                    transcribed = gladia.transcribe(audio_url, language="ja")

                    if transcribed:
                        st.session_state.transcribed_text = transcribed
                        st.info(f"文字起こし完了: {len(transcribed)}文字")
                        progress_bar.progress(60)

                        try:
                            formatted = gemini.format_text(transcribed)
                        except Exception as e:
                            error_str = str(e)
                            if "429" in error_str or "quota" in error_str.lower():
                                st.error("⚠️ Gemini APIのクォータ（利用制限）を超過しました")
                                st.warning("30秒後に再試行するか、新しいAPIキーを取得してください: https://aistudio.google.com/apikey")
                            else:
                                st.error(f"テキスト整形エラー: {type(e).__name__}: {e}")
                            formatted = None

                        if formatted:
                            formatted = split_long_lines(formatted)
                            st.session_state.formatted_text = formatted
                            progress_bar.progress(80)
                            filename = gemini.generate_filename(formatted)
                            st.session_state.filename = filename or "output"
                            progress_bar.progress(100)
                            st.success("Complete!")
                        else:
                            st.error("テキスト整形に失敗しました")
                            # 文字起こしテキストをそのまま使用するオプション
                            st.warning("文字起こしテキストをそのまま使用します（手動で整形してください）")
                            st.session_state.formatted_text = transcribed
                            st.session_state.filename = "output"
                    else:
                        st.error("文字起こしに失敗しました")
                else:
                    st.error("ファイルアップロードに失敗しました")
                    st.warning("考えられる原因: APIキーの有効期限切れ、ファイルサイズ制限、ネットワークエラー")
            finally:
                # 処理完了後に一時ファイルを削除
                if os.path.exists(tmp_file_path):
                    os.unlink(tmp_file_path)

with tab2:
    st.subheader("テキストファイルアップロード")

    _uc = st.session_state.get('upload_counter', 0)
    text_file = st.file_uploader(
        "テキストファイルを選択してください (.txt)",
        type=["txt"],
        key=f"text_file_uploader_{_uc}"
    )

    if text_file is not None:
        st.info(f"アップロードされたファイル: {text_file.name}")

        if st.button("START", key="text_process_btn"):
            try:
                progress_bar = st.progress(0)

                progress_bar.progress(20)
                raw_text = text_file.read().decode('utf-8', errors='replace')

                if raw_text.strip():
                    st.session_state.transcribed_text = raw_text
                    progress_bar.progress(50)

                    # テキスト整形：改行ごとに句読点を追加
                    lines = raw_text.strip().split('\n')
                    formatted_lines = []
                    punctuation = ('。', '、', '！', '？', '!', '?', '．', '，')

                    for i, line in enumerate(lines):
                        line = line.strip()
                        if not line:
                            continue
                        # 既に句読点で終わっている場合はそのまま
                        if line.endswith(punctuation):
                            formatted_lines.append(line)
                        else:
                            # 最後の行は「。」、それ以外は「、」
                            if i == len(lines) - 1:
                                formatted_lines.append(line + '。')
                            else:
                                formatted_lines.append(line + '、')

                    formatted_text = '\n'.join(formatted_lines)
                    formatted_text = split_long_lines(formatted_text)
                    st.session_state.formatted_text = formatted_text
                    progress_bar.progress(80)

                    filename = os.path.splitext(text_file.name)[0]
                    st.session_state.filename = filename
                    progress_bar.progress(100)
                    st.success("Complete!")
                else:
                    st.error("テキストファイルが空です")
            except Exception as e:
                st.error(f"テキスト読み込みエラー: {str(e)}")

with tab3:
    st.subheader("テキストを直接入力")

    direct_text = st.text_area(
        "テキストを貼り付けてください（自動整形されます）",
        height=250,
        placeholder="ここにテキストを貼り付け...\n\n例：\nこれもちょっとした誤解で\n落とし穴がいっぱいあるのです",
        key="direct_text_input"
    )

    if st.button("START", key="direct_text_btn"):
        if direct_text.strip():
            progress_bar = st.progress(0)
            progress_bar.progress(20)

            # テキスト整形：改行ごとに句読点を追加
            lines = direct_text.strip().split('\n')
            formatted_lines = []
            punctuation = ('。', '、', '！', '？', '!', '?', '．', '，')

            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                if line.endswith(punctuation):
                    formatted_lines.append(line)
                else:
                    if i == len(lines) - 1:
                        formatted_lines.append(line + '。')
                    else:
                        formatted_lines.append(line + '、')

            formatted_text = '\n'.join(formatted_lines)
            formatted_text = split_long_lines(formatted_text)
            st.session_state.formatted_text = formatted_text
            st.session_state.transcribed_text = direct_text
            progress_bar.progress(50)

            # ファイル名生成
            if gemini:
                # Gemini APIでファイル名を生成
                filename = gemini.generate_filename(formatted_text)
                st.session_state.filename = filename or "output"
            else:
                # テキストの最初の行から自動生成（句読点除去、最大20文字）
                first_line = formatted_lines[0] if formatted_lines else "output"
                clean_name = first_line.replace('、', '').replace('。', '').replace('！', '').replace('？', '')
                st.session_state.filename = clean_name[:20] if len(clean_name) > 20 else clean_name

            progress_bar.progress(100)
            st.success("Complete!")
        else:
            st.error("テキストを入力してください")

with tab4:
    st.subheader("音声アップロード")
    st.info("外部TTSで生成した音声をアップロード → 自動で文字起こし＆整形 → 動画生成（動画から生成と同じフロー）")

    # 1. 音声アップロード → 自動で文字起こし＆整形
    st.markdown("### 1. 音声ファイルをアップロード")
    _uc = st.session_state.get('upload_counter', 0)
    uploaded_audio = st.file_uploader(
        "音声ファイルを選択（アップロード後、自動で文字起こし＆整形）",
        type=["wav", "mp3", "m4a", "aac", "ogg"],
        accept_multiple_files=False,
        key=f"audio_uploader_{_uc}"
    )

    if uploaded_audio and not st.session_state.get('audio_upload_mode'):
        # 新しい音声がアップロードされたら自動で処理開始
        st.success(f"アップロード: {uploaded_audio.name}")
        st.audio(uploaded_audio, format=f"audio/{uploaded_audio.name.split('.')[-1]}")

        audio_filename = os.path.splitext(uploaded_audio.name)[0]

        if not gladia_api_key:
            st.error("API設定でGladia APIキーを入力してください")
        elif not gemini_api_key:
            st.error("API設定でGemini APIキーを入力してください（テキスト整形に必要）")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()

            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{uploaded_audio.name.split('.')[-1]}") as tmp_file:
                tmp_file.write(uploaded_audio.read())
                tmp_audio_path = tmp_file.name
            uploaded_audio.seek(0)

            try:
                # Step 1: Gladia文字起こし
                status_text.text("音声を文字起こし中（Gladia API）...")
                progress_bar.progress(10)

                result = gladia.transcribe_from_file_with_timestamps(tmp_audio_path, language="ja")

                if result and result.get("segments"):
                    gladia_segments = result["segments"]
                    gladia_words = result.get("words", [])  # 単語レベルのタイムスタンプ
                    raw_text = ' '.join([seg['text'] for seg in gladia_segments])
                    progress_bar.progress(40)
                    status_text.text(f"文字起こし完了: {len(gladia_segments)} セグメント, {len(gladia_words)} 単語")

                    # Step 2: Geminiで整形（動画から生成と同じ）
                    status_text.text("テキストを整形中（Gemini API）...")
                    progress_bar.progress(50)

                    formatted_text = gemini.format_text(raw_text)

                    if formatted_text:
                        progress_bar.progress(70)

                        # ファイル名生成
                        status_text.text("ファイル名を生成中...")
                        generated_filename = gemini.generate_filename(formatted_text)
                        if generated_filename:
                            audio_filename = generated_filename

                        progress_bar.progress(100)
                        status_text.text("Complete!")

                        # セッションに保存（単語リストも保存）
                        st.session_state.timestamped_segments = gladia_segments
                        st.session_state.gladia_words = gladia_words  # 単語レベルのタイムスタンプ
                        st.session_state.audio_file_data = uploaded_audio.read()
                        uploaded_audio.seek(0)
                        st.session_state.audio_file_ext = os.path.splitext(uploaded_audio.name)[1]  # .wav, .mp3等
                        st.session_state.filename = audio_filename
                        st.session_state.audio_upload_mode = True
                        st.session_state.audio_text_editor = split_long_lines(formatted_text)

                        st.success(f"Complete! 整形済みテキスト生成完了（{len(gladia_words)}単語のタイムスタンプ取得）")
                        st.rerun()
                    else:
                        st.error("テキスト整形に失敗しました")
                else:
                    st.error("文字起こしに失敗しました")

                if os.path.exists(tmp_audio_path):
                    os.unlink(tmp_audio_path)

            except Exception as e:
                st.error(f"エラー: {str(e)}")
                import traceback
                st.code(traceback.format_exc())
                if os.path.exists(tmp_audio_path):
                    os.unlink(tmp_audio_path)

    # 2. テキスト編集（動画から生成と同じUI）
    if st.session_state.get('audio_text_editor') and st.session_state.get('audio_upload_mode'):
        st.markdown("---")
        st.markdown("### 2. テキストを確認・編集")

        edited_text = st.text_area(
            "整形済みテキスト（1行14文字以内、句読点で終わる）",
            value=st.session_state.audio_text_editor,
            height=300,
            key="audio_text_area"
        )
        st.session_state.audio_text_editor = edited_text

        # 行数カウント
        lines = [line.strip() for line in edited_text.strip().split('\n') if line.strip()]
        word_count = len(st.session_state.gladia_words) if st.session_state.get('gladia_words') else 0

        st.success(f"**{len(lines)}行** / {word_count}単語のタイムスタンプで同期")

        # はみ出しバリデーション
        audio_overflow = check_line_overflow(edited_text)
        if audio_overflow:
            st.error(f"テロップはみ出し: {len(audio_overflow)}行が{MAX_DISPLAY_CHARS}表示文字を超えています。修正してください。")
            for line_no, display_cnt, line_text in audio_overflow:
                st.warning(f"行{line_no}: {display_cnt}文字 → 「{line_text}」")

        # 3. 動画生成
        st.markdown("---")
        st.markdown("### 3. 動画を生成")

        if st.button("GENERATE VIDEO", key="generate_audio_upload_video_btn", disabled=bool(audio_overflow)):
            try:
                progress_bar = st.progress(0)
                status_text = st.empty()

                status_text.text("タイムスタンプを計算中...")
                progress_bar.progress(5)

                # テキストを行に分割
                lines = [line.strip() for line in edited_text.strip().split('\n') if line.strip()]
                gladia_words = st.session_state.get('gladia_words', [])

                if gladia_words:
                    # 単語レベルのタイムスタンプを使用
                    segments = calculate_line_timestamps(lines, gladia_words)
                    validate_segments(segments)
                    status_text.text(f"単語レベルのタイムスタンプで同期: {len(segments)}行")
                else:
                    # フォールバック: 均等分割
                    gladia_segments = st.session_state.timestamped_segments
                    total_start = gladia_segments[0]['start']
                    total_end = gladia_segments[-1]['end']
                    total_duration = total_end - total_start
                    segment_duration = total_duration / len(lines) if len(lines) > 0 else 1

                    segments = []
                    for i, text in enumerate(lines):
                        start_time = total_start + (i * segment_duration)
                        end_time = total_start + ((i + 1) * segment_duration)
                        segments.append({
                            "start": start_time,
                            "end": end_time,
                            "text": text
                        })

                progress_bar.progress(10)

                # 一時ファイルに音声を保存（元の拡張子を維持）
                audio_ext = st.session_state.get('audio_file_ext', '.wav')
                with tempfile.NamedTemporaryFile(delete=False, suffix=audio_ext) as tmp_file:
                    tmp_file.write(st.session_state.audio_file_data)
                    tmp_audio_path = tmp_file.name

                def update_progress(current, total, message):
                    progress = int(10 + (current / total) * 85)
                    progress_bar.progress(progress)

                video_gen = VideoGeneratorFFmpeg(
                    background_color=(0, 255, 0),
                    voicevox_url=voicevox_url
                )

                video_transparent, video_preview = video_gen.create_video_from_timestamped_segments(
                    audio_path=tmp_audio_path,
                    segments=segments,
                    width=1080,
                    height=1920,
                    transparent=True,
                    progress_callback=update_progress
                )

                os.unlink(tmp_audio_path)

                if video_transparent:
                    st.session_state.generated_video = video_transparent
                    st.session_state.preview_video = video_preview
                    progress_bar.progress(100)
                    status_text.text("動画生成完了！")
                    st.rerun()

            except Exception as e:
                st.error(f"動画生成エラー: {str(e)}")
                import traceback
                st.code(traceback.format_exc())

    # プレビューとダウンロード
    if st.session_state.get('generated_video') and st.session_state.get('preview_video') and st.session_state.get('audio_upload_mode'):
        st.markdown("---")
        st.subheader("プレビュー")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            video_base64 = base64.b64encode(st.session_state.preview_video).decode()
            st.markdown(f'''
            <div class="iphone-frame">
                <div class="iphone-device">
                    <div class="iphone-dynamic-island"></div>
                    <div class="iphone-screen">
                        <video controls playsinline>
                            <source src="data:video/mp4;base64,{video_base64}" type="video/mp4">
                        </video>
                    </div>
                    <div class="iphone-home-indicator"></div>
                </div>
            </div>
            ''', unsafe_allow_html=True)

        st.info("プレビューはチェッカー背景で表示。ダウンロードは透過動画（MOV）です。")
        st.caption(f"🔤 使用フォント: **{_font_info['name']}**（{_font_info['size']}px）")

        st.download_button(
            label="DOWNLOAD VIDEO (.mov)",
            data=st.session_state.generated_video,
            file_name=f"{st.session_state.filename}.mov",
            mime="video/quicktime",
            key="download_audio_upload_video",
            disabled=bool(audio_overflow)
        )

        # SNSコンテンツ生成
        st.markdown("---")
        st.subheader("タイトル・紹介文・ハッシュタグ生成")

        if st.button("GENERATE SNS", key="generate_sns_audio_upload_btn"):
            if not gemini_api_key:
                st.error("API設定でGemini APIキーを入力してください")
            elif not st.session_state.audio_text_editor:
                st.error("テキストが見つかりません")
            else:
                progress_bar = st.progress(0)
                progress_bar.progress(30)
                sns_content = gemini.generate_metadata(st.session_state.audio_text_editor)
                progress_bar.progress(90)
                if sns_content:
                    st.session_state.audio_upload_sns_content = sns_content
                    progress_bar.progress(100)
                    st.rerun()

        if st.session_state.get('audio_upload_sns_content'):
            st.markdown("**生成されたコンテンツ（編集可能）**")
            sns_editor = st.text_area(
                "タイトル・紹介文・ハッシュタグ",
                value=st.session_state.audio_upload_sns_content,
                height=300,
                key="audio_upload_sns_editor"
            )

            # 全テキストをまとめてダウンロード
            full_text = "【整形テキスト】\n" + st.session_state.audio_text_editor
            full_text += "\n\n" + sns_editor

            st.download_button(
                label="DOWNLOAD ALL TEXT",
                data=full_text,
                file_name=f"{st.session_state.filename}_full.txt",
                mime="text/plain",
                key="download_audio_upload_full_text"
            )

# セクション2: 整形済みテキスト表示
if st.session_state.formatted_text:
    st.header("2. テキスト編集")

    if "text_editor" not in st.session_state:
        st.session_state.text_editor = st.session_state.formatted_text

    if "filename" not in st.session_state or not st.session_state.filename:
        st.session_state.filename = "output"

    # テキストダウンロード用のフォーマット関数
    def format_text_for_download(text: str, target_length: int = 14) -> str:
        lines = text.split('\n')
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            chunks = []
            current_chunk = ""
            for char in line:
                if char in ['。', '、']:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""
                else:
                    current_chunk += char
            if current_chunk:
                chunks.append(current_chunk)

            current_line = ""
            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                if not current_line:
                    current_line = chunk
                    continue
                combined_len = len(current_line + chunk)
                if combined_len > target_length + 4:
                    new_lines.append(current_line)
                    current_line = chunk
                elif abs(target_length - combined_len) <= abs(target_length - len(current_line)):
                    current_line += chunk
                else:
                    new_lines.append(current_line)
                    current_line = chunk
            if current_line:
                new_lines.append(current_line)
        return '\n'.join(new_lines)

    # 2カラムレイアウト：整形テキスト（左）とひらがな（右）
    col_text, col_hiragana = st.columns(2)

    with col_text:
        st.subheader("整形済みテキスト（動画表示用）")
        # text_areaの値を明示的に取得して保存
        current_text = st.text_area(
            "整形されたテキスト",
            value=st.session_state.get("text_editor", st.session_state.formatted_text),
            height=400,
            key="text_editor_widget"
        )
        # 編集されたテキストをセッションに保存
        st.session_state.text_editor = current_text

        # はみ出しバリデーション
        text_overflow = check_line_overflow(current_text)
        if text_overflow:
            st.error(f"テロップはみ出し: {len(text_overflow)}行が{MAX_DISPLAY_CHARS}表示文字を超えています。修正してください。")
            for line_no, display_cnt, line_text in text_overflow:
                st.warning(f"行{line_no}: {display_cnt}文字 → 「{line_text}」")

        formatted_main_text = format_text_for_download(current_text)
        st.download_button(
            label="DOWNLOAD TEXT",
            data=formatted_main_text,
            file_name=f"{st.session_state.filename}.txt",
            mime="text/plain",
            key="download_text",
            disabled=bool(text_overflow)
        )

    with col_hiragana:
        st.subheader("ひらがな（音声生成用）")

        # ひらがなテキストを表示
        if st.session_state.hiragana_text:
            if "hiragana_editor" not in st.session_state:
                st.session_state.hiragana_editor = st.session_state.hiragana_text

            st.text_area("ひらがなテキスト（編集可能）", height=400, key="hiragana_editor")

            if st.button("再変換", key="convert_hiragana_btn"):
                if not gemini_api_key:
                    st.error("Gemini APIキーを入力してください")
                else:
                    with st.spinner("変換中..."):
                        hiragana_result = gemini.convert_to_hiragana(st.session_state.text_editor)
                        if hiragana_result:
                            st.session_state.hiragana_text = hiragana_result
                            if "hiragana_editor" in st.session_state:
                                del st.session_state.hiragana_editor
                            st.rerun()
                        else:
                            st.error("変換失敗")
        else:
            st.text_area("ひらがなテキスト", value="", height=400, disabled=True, key="hiragana_placeholder")

            if st.button("ひらがなに変換", key="convert_hiragana_btn_init"):
                if not gemini_api_key:
                    st.error("Gemini APIキーを入力してください")
                else:
                    with st.spinner("変換中..."):
                        hiragana_result = gemini.convert_to_hiragana(st.session_state.text_editor)
                        if hiragana_result:
                            st.session_state.hiragana_text = hiragana_result
                            if "hiragana_editor" in st.session_state:
                                del st.session_state.hiragana_editor
                            st.rerun()
                        else:
                            st.error("ひらがな変換に失敗しました")

    # ファイル名入力
    final_filename = st.text_input("ファイル名（編集可能）", value=st.session_state.filename, key="filename_input")

    # セクション3: 音声アップロード＆動画生成
    st.header("3. 音声アップロード＆動画生成")
    st.info("外部TTSで生成した音声をアップロードして動画を生成します")

    # 音声アップロード
    _uc = st.session_state.get('upload_counter', 0)
    uploaded_audio_sec3 = st.file_uploader(
        "音声ファイルを選択",
        type=["wav", "mp3", "m4a", "aac", "ogg"],
        key=f"audio_uploader_sec3_{_uc}"
    )

    if uploaded_audio_sec3:
        st.audio(uploaded_audio_sec3, format=f"audio/{uploaded_audio_sec3.name.split('.')[-1]}")

        if st.button("GENERATE VIDEO", key="generate_video_sec3_btn", disabled=bool(text_overflow)):
            try:
                progress_bar = st.progress(0)
                status_text = st.empty()

                status_text.text("音声ファイルを処理中...")
                progress_bar.progress(10)

                # 音声ファイルを一時保存
                uploaded_audio_sec3.seek(0)
                audio_data = uploaded_audio_sec3.read()

                with tempfile.NamedTemporaryFile(delete=False, suffix=f".{uploaded_audio_sec3.name.split('.')[-1]}") as tmp_file:
                    tmp_file.write(audio_data)
                    tmp_audio_path = tmp_file.name

                # テキストを行に分割
                display_text = st.session_state.text_editor
                lines = [line.strip() for line in display_text.strip().split('\n') if line.strip()]

                status_text.text("文字起こし中（タイムスタンプ取得）...")
                progress_bar.progress(20)

                # Gladiaで音声のタイムスタンプを取得
                if gladia_api_key:
                    try:
                        result = gladia.transcribe_from_file_with_timestamps(tmp_audio_path, language="ja")
                    except Exception as e:
                        st.error(f"Gladia API エラー: {e}")
                        os.unlink(tmp_audio_path)
                        st.stop()

                    if result and result.get("words"):
                        gladia_words = result["words"]

                        # 文字レベル補間で各行のタイミングを計算（Tab 4と同じアルゴリズム）
                        segments = calculate_line_timestamps(lines, gladia_words)
                        validate_segments(segments)

                        status_text.text(f"タイムスタンプ取得完了: {len(segments)}行")
                    else:
                        if result is None:
                            st.error("タイムスタンプの取得に失敗しました（音声アップロードまたは文字起こしエラー）")
                        elif not result.get("words"):
                            st.error("タイムスタンプの取得に失敗しました（単語データが空です）")
                            if result.get("segments"):
                                st.warning(f"セグメントは取得できました: {len(result['segments'])}個")
                        else:
                            st.error("タイムスタンプの取得に失敗しました")
                        st.warning("考えられる原因: APIキーの有効期限切れ、音声ファイル形式、ネットワークエラー")
                        os.unlink(tmp_audio_path)
                        st.stop()
                else:
                    st.error("Gladia APIキーを設定してください")
                    os.unlink(tmp_audio_path)
                    st.stop()

                progress_bar.progress(40)
                status_text.text("動画を生成中...")

                def update_progress(current, total, message):
                    progress = int(40 + (current / total) * 50)
                    progress_bar.progress(progress)

                video_gen = VideoGeneratorFFmpeg(
                    background_color=(0, 255, 0),
                    voicevox_url=voicevox_url
                )

                video_transparent, video_preview = video_gen.create_video_from_timestamped_segments(
                    audio_path=tmp_audio_path,
                    segments=segments,
                    width=1080,
                    height=1920,
                    transparent=True,
                    progress_callback=update_progress
                )

                os.unlink(tmp_audio_path)

                if video_transparent:
                    st.session_state.generated_video = video_transparent
                    st.session_state.preview_video = video_preview
                    progress_bar.progress(100)
                    status_text.text("動画生成完了！")
                    st.rerun()

            except Exception as e:
                st.error(f"動画生成エラー: {str(e)}")
                import traceback
                st.code(traceback.format_exc())

    # 動画プレビューとダウンロード
    if st.session_state.get('generated_video') and st.session_state.get('preview_video'):
        st.subheader("プレビュー")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            video_base64 = base64.b64encode(st.session_state.preview_video).decode()
            st.markdown(f'''
            <div class="iphone-frame">
                <div class="iphone-device">
                    <div class="iphone-dynamic-island"></div>
                    <div class="iphone-screen">
                        <video controls playsinline>
                            <source src="data:video/mp4;base64,{video_base64}" type="video/mp4">
                        </video>
                    </div>
                    <div class="iphone-home-indicator"></div>
                </div>
            </div>
            ''', unsafe_allow_html=True)

        st.info("プレビューはチェッカー背景で表示。ダウンロードは透過動画（MOV）です。")
        st.caption(f"🔤 使用フォント: **{_font_info['name']}**（{_font_info['size']}px）")

        st.download_button(
            label="DOWNLOAD VIDEO (.mov)",
            data=st.session_state.generated_video,
            file_name=f"{final_filename}.mov",
            mime="video/quicktime",
            key="download_video_sec3"
        )

    # セクション4: SNSコンテンツ生成
    st.header("4. タイトル・紹介文・ハッシュタグ生成")

    if st.button("GENERATE SNS", key="generate_sns_content_btn"):
        if not gemini_api_key:
            st.error("サイドバーでGemini APIキーを入力してください")
        elif not st.session_state.text_editor:
            st.error("テキストが見つかりません")
        else:
            progress_bar = st.progress(0)
            progress_bar.progress(30)
            sns_content = gemini.generate_metadata(st.session_state.text_editor)
            progress_bar.progress(90)
            if sns_content:
                st.session_state.generated_sns_content = sns_content
                progress_bar.progress(100)

    if st.session_state.generated_sns_content:
        st.subheader("生成されたコンテンツ（編集可能）")
        if "sns_content_editor" not in st.session_state:
            st.session_state.sns_content_editor = st.session_state.generated_sns_content
        st.text_area("タイトル・紹介文・ハッシュタグ", height=400, key="sns_content_editor")

        # 全テキストをまとめてダウンロード
        full_text = "【整形テキスト】\n" + formatted_main_text

        # 言い換えテキストがあれば追加
        if st.session_state.rephrased_result:
            full_text += "\n\n【言い換えテキスト】\n" + st.session_state.rephrased_result

        full_text += "\n\n" + st.session_state.sns_content_editor

        st.download_button(
            label="DOWNLOAD ALL TEXT",
            data=full_text,
            file_name=f"{final_filename}_full.txt",
            mime="text/plain",
            key="download_full_text"
        )

# フッター
st.markdown("---")
st.markdown("Made with Streamlit, Gladia API, Gemini API, and FFmpeg | **v3**")
