import os
import platform
import re
import shutil
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFont
from utils.text_normalize import normalize_for_timing, remove_punctuation_for_display
from utils.voicevox import VoiceVoxAPI


def _find_binary(name):
    """バイナリの絶対パスを取得"""
    # 1. shutil.which
    path = shutil.which(name)
    if path:
        return path
    # 2. 固定パス候補
    for candidate in [f'/usr/bin/{name}', f'/usr/local/bin/{name}', f'/snap/bin/{name}']:
        if os.path.isfile(candidate):
            return candidate
    # 3. imageio-ffmpeg（ffmpegのみ）
    if name == 'ffmpeg':
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    # 4. imageio-ffmpegのffmpegと同じディレクトリ
    if name == 'ffprobe' and FFMPEG_BIN:
        try:
            candidate = os.path.join(os.path.dirname(FFMPEG_BIN), 'ffprobe')
            if os.path.isfile(candidate):
                return candidate
        except Exception:
            pass
    return None


FFMPEG_BIN = _find_binary('ffmpeg') or 'ffmpeg'
FFPROBE_BIN = _find_binary('ffprobe')
print(f"[INFO] ffmpeg: {FFMPEG_BIN}, ffprobe: {FFPROBE_BIN}")


class VideoGeneratorFFmpeg:
    """Pillow + FFmpegで動画生成（高速・高品質）"""

    # 縦書き時に90°反時計回り(CCW)で回転する文字（ー→縦棒）
    VERTICAL_ROTATE_CHARS = {'ー', '〜', '～', '－', '-', '―', '‐', '–', '—', '=', '＝', '+', '＋', '<', '>', '＜', '＞'}

    # 縦書き時に90°時計回り(CW)で回転する括弧類（向きを正しくするため逆回転）
    VERTICAL_ROTATE_BRACKETS = {'【', '】', '「', '」', '『', '』', '（', '）', '(', ')', '[', ']', '［', '］', '〈', '〉', '《', '》', '〔', '〕'}

    # 小書き文字（右に寄せる）
    SMALL_CHARS = {'っ', 'ぁ', 'ぃ', 'ぅ', 'ぇ', 'ぉ', 'ゃ', 'ゅ', 'ょ', 'ゎ',
                   'ッ', 'ァ', 'ィ', 'ゥ', 'ェ', 'ォ', 'ャ', 'ュ', 'ョ', 'ヮ', 'ヶ', 'ヵ'}

    def __init__(self, background_color=(0, 255, 0), voicevox_url="http://localhost:50021"):
        self.background_color = background_color
        self.voicevox = VoiceVoxAPI(voicevox_url)

    @staticmethod
    def get_current_font_info():
        """現在使用されるフォント名とパスを返す"""
        bundled_font = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fonts', 'NotoSansJP-Bold.otf')
        font_paths = [bundled_font]
        if platform.system() == "Darwin":
            font_paths.extend([
                "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
                "/System/Library/Fonts/ヒラギノ角ゴ ProN W6.otf",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
            ])
        elif platform.system() == "Linux":
            font_paths.extend([
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
            ])
        else:
            font_paths.extend([
                "C:/Windows/Fonts/YuGothB.ttc",
                "C:/Windows/Fonts/YuGothM.ttc",
                "C:/Windows/Fonts/meiryo.ttc",
            ])
        for font_path in font_paths:
            try:
                ImageFont.truetype(font_path, 10)
                font_name = os.path.basename(font_path)
                return {"name": font_name, "path": font_path, "size": 100}
            except:
                continue
        return {"name": "デフォルトフォント", "path": None, "size": 100}

    def _create_checker_background(self, width: int, height: int, cell_size: int = 20) -> Image.Image:
        """チェッカーパターン背景を作成（Photoshop風透過表示）"""
        img = Image.new('RGB', (width, height))
        draw = ImageDraw.Draw(img)

        color1 = (200, 200, 200)  # 明るいグレー
        color2 = (150, 150, 150)  # 暗いグレー

        for y in range(0, height, cell_size):
            for x in range(0, width, cell_size):
                # 市松模様
                if (x // cell_size + y // cell_size) % 2 == 0:
                    color = color1
                else:
                    color = color2
                draw.rectangle([x, y, x + cell_size, y + cell_size], fill=color)

        return img

    def _create_text_image(self, text: str, width: int, height: int, font_size: int = 100, transparent: bool = False, checker: bool = False) -> Image.Image:
        """縦書きテキスト画像を生成"""
        # バンドル版フォントのパス（最優先）
        bundled_font = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fonts', 'NotoSansJP-Bold.otf')
        font = None
        # 1. バンドル版フォント（全環境で確実に動作）
        font_paths = [bundled_font]
        # 2. OS別のシステムフォント
        if platform.system() == "Darwin":
            font_paths.extend([
                "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
                "/System/Library/Fonts/ヒラギノ角ゴ ProN W6.otf",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
            ])
        elif platform.system() == "Linux":
            font_paths.extend([
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
            ])
        else:
            font_paths.extend([
                "C:/Windows/Fonts/YuGothB.ttc",
                "C:/Windows/Fonts/YuGothM.ttc",
                "C:/Windows/Fonts/meiryo.ttc",
            ])
        for font_path in font_paths:
            try:
                font = ImageFont.truetype(font_path, font_size)
                print(f"[INFO] フォント読み込み成功: {font_path}")
                break
            except:
                continue
        if font is None:
            print(f"[WARNING] CJKフォントが見つかりません。検索パス: {font_paths[:10]}...")
            font = ImageFont.load_default()

        # 固定の文字送り（通常）
        char_pitch = font_size

        # 文字情報を収集
        char_info = []
        for char in text:
            needs_rotation = char in self.VERTICAL_ROTATE_CHARS or char in self.VERTICAL_ROTATE_BRACKETS
            is_small = char in self.SMALL_CHARS
            char_info.append((char, needs_rotation, is_small))

        # 高さ計算（全文字同じ間隔）
        total_height = char_pitch * len(char_info)
        max_width = font_size

        # 背景サイズ
        rect_width = max_width + 60
        rect_height = total_height + 60

        # 画像を作成（透過、チェッカー、または緑背景）
        if transparent:
            img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        elif checker:
            img = self._create_checker_background(width, height)
        else:
            img = Image.new('RGB', (width, height), self.background_color)
        draw = ImageDraw.Draw(img)

        # 白い長方形を描画（Y=288）- テキストが空の場合はスキップ
        rect_x = (width - rect_width) // 2
        rect_y = 288
        if char_info:
            draw.rectangle(
                [(rect_x, rect_y), (rect_x + rect_width, rect_y + rect_height)],
                fill=(255, 255, 255)
            )

        # 縦書きテキスト描画（白背景はそのまま、文字のみ25px上）
        y_offset = rect_y + 5
        x_center = width // 2

        for i, (char, needs_rotation, is_small) in enumerate(char_info):
            if needs_rotation:
                # 長音記号を90度回転（中央配置）
                # 回転用の画像サイズ
                img_size = font_size * 2
                char_img = Image.new('RGBA', (img_size, img_size), (0, 0, 0, 0))
                char_draw = ImageDraw.Draw(char_img)

                # 文字を画像の中央に描画
                char_draw.text((img_size // 2, img_size // 2), char, font=font, fill=(0, 0, 0, 255), anchor="mm")

                # 括弧は時計回り(-90°)、その他は反時計回り(90°)
                angle = -90 if char in self.VERTICAL_ROTATE_BRACKETS else 90
                char_img = char_img.rotate(angle, expand=False, resample=Image.BICUBIC)

                # 回転後の文字位置を取得して中央揃え
                rotated_bbox = char_img.getbbox()
                if rotated_bbox:
                    rotated_center_x = (rotated_bbox[0] + rotated_bbox[2]) // 2
                    rotated_center_y = (rotated_bbox[1] + rotated_bbox[3]) // 2
                    # x方向：文字の中心をx_centerに
                    paste_x = x_center - rotated_center_x
                    # y方向：文字の中心を文字スロットの中心に + 調整オフセット
                    slot_center_y = y_offset + char_pitch // 2
                    paste_y = slot_center_y - rotated_center_y + font_size // 4
                else:
                    paste_x = x_center - img_size // 2
                    paste_y = y_offset + font_size // 4

                img.paste(char_img, (paste_x, paste_y), char_img)
                y_offset += char_pitch
            elif is_small:
                # 小書き文字は右に寄せ、前の文字に重ねる
                # 前の文字が小書き文字かどうかで重なり量を変える
                prev_is_small = i > 0 and char_info[i-1][2]
                overlap = 10 if prev_is_small else 20  # 2文字目以降は10px、1文字目は20px

                bbox = draw.textbbox((0, 0), char, font=font)
                char_w = bbox[2] - bbox[0]
                # 通常文字と同じ中央揃えから10px右にオフセット
                x = x_center - char_w // 2 + 10
                y = y_offset - overlap
                draw.text((x, y), char, font=font, fill=(0, 0, 0))
                y_offset += char_pitch - overlap
            else:
                # 通常の文字
                bbox = draw.textbbox((0, 0), char, font=font)
                char_w = bbox[2] - bbox[0]
                x = x_center - char_w // 2
                y = y_offset
                draw.text((x, y), char, font=font, fill=(0, 0, 0))
                y_offset += char_pitch

        return img

    def count_clips(self, text: str) -> int:
        """テキストから生成されるクリップ数を計算"""
        lines = [line.strip() for line in text.strip().split('\n') if line.strip()]
        return len(lines)

    def create_green_screen_video(
        self,
        audio_text: str,
        display_text: str,
        speaker_id: int,
        speed: float = 1.0,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        transparent: bool = False,
        progress_callback=None
    ) -> tuple:
        """FFmpegで動画を生成

        Args:
            audio_text: 音声生成用テキスト（改行区切り）
            display_text: 表示用テキスト（改行区切り）
            progress_callback: 進捗コールバック関数 (current, total, message) を受け取る

        Returns:
            tuple: (透過動画bytes, プレビュー動画bytes) - 透過モード時
                   (動画bytes, None) - 非透過モード時
        """
        # 音声用テキストを行ごとに分割
        audio_lines = [line.strip() for line in audio_text.strip().split('\n') if line.strip()]

        # 表示用テキストを行ごとに分割し、句読点を削除
        display_lines = []
        for line in display_text.strip().split('\n'):
            line = line.strip()
            if line:
                display_lines.append(remove_punctuation_for_display(line))

        # 行数が異なる場合は警告し、短い方に合わせる
        if len(audio_lines) != len(display_lines):
            print(f"警告: 音声用テキスト({len(audio_lines)}行)と表示用テキスト({len(display_lines)}行)の行数が異なります")
            min_len = min(len(audio_lines), len(display_lines))
            audio_lines = audio_lines[:min_len]
            display_lines = display_lines[:min_len]

        # (音声用, 表示用) のペアを作成
        lines = list(zip(audio_lines, display_lines))

        if not lines:
            raise ValueError("テキストが空です")

        total_clips = len(lines)

        temp_dir = tempfile.mkdtemp()
        temp_files = []
        segment_videos_transparent = []
        segment_videos_preview = []

        try:
            # 各行の動画セグメントを作成
            for i, (audio_line, display_line) in enumerate(lines):
                clip_num = i + 1
                print(f"セグメント {clip_num}/{total_clips} を作成中: {display_line[:20]}...")

                # 進捗コールバック
                if progress_callback:
                    progress_callback(clip_num, total_clips, f"クリップ {clip_num}/{total_clips} を生成中...")

                # 1. 音声生成
                audio_data = self.voicevox.generate_voice(audio_line, speaker_id, speed)
                if not audio_data:
                    raise Exception(f"行 {i+1} の音声生成に失敗しました")

                audio_path = os.path.join(temp_dir, f"audio_{i}.wav")
                with open(audio_path, 'wb') as f:
                    f.write(audio_data)
                temp_files.append(audio_path)

                # 2. 音声の長さを取得
                duration = self._get_audio_duration(audio_path)

                if transparent:
                    # 透過モード：透過画像とチェッカー画像の両方を生成
                    # 透過画像
                    img_transparent = self._create_text_image(display_line, width, height, transparent=True)
                    img_transparent_path = os.path.join(temp_dir, f"frame_transparent_{i}.png")
                    img_transparent.save(img_transparent_path)
                    temp_files.append(img_transparent_path)

                    # チェッカー背景画像（プレビュー用）
                    img_preview = self._create_text_image(display_line, width, height, checker=True)
                    img_preview_path = os.path.join(temp_dir, f"frame_preview_{i}.png")
                    img_preview.save(img_preview_path)
                    temp_files.append(img_preview_path)

                    # 透過動画セグメント
                    video_transparent_path = os.path.join(temp_dir, f"segment_transparent_{i}.mov")
                    self._create_video_segment(img_transparent_path, audio_path, video_transparent_path, duration, fps, transparent=True)
                    segment_videos_transparent.append(video_transparent_path)
                    temp_files.append(video_transparent_path)

                    # プレビュー動画セグメント（MP4）
                    video_preview_path = os.path.join(temp_dir, f"segment_preview_{i}.mp4")
                    self._create_video_segment(img_preview_path, audio_path, video_preview_path, duration, fps, transparent=False)
                    segment_videos_preview.append(video_preview_path)
                    temp_files.append(video_preview_path)
                else:
                    # 非透過モード：グリーンバック動画のみ
                    img = self._create_text_image(display_line, width, height, transparent=False)
                    img_path = os.path.join(temp_dir, f"frame_{i}.png")
                    img.save(img_path)
                    temp_files.append(img_path)

                    video_path = os.path.join(temp_dir, f"segment_{i}.mp4")
                    self._create_video_segment(img_path, audio_path, video_path, duration, fps, transparent=False)
                    segment_videos_transparent.append(video_path)
                    temp_files.append(video_path)

            # 全セグメントを連結
            print(f"全 {len(segment_videos_transparent)} セグメントを連結中...")

            if transparent:
                # 透過動画（MOV）
                output_transparent_path = os.path.join(temp_dir, "output_transparent.mov")
                self._concat_videos(segment_videos_transparent, output_transparent_path, transparent=True)
                temp_files.append(output_transparent_path)

                # プレビュー動画（MP4）
                output_preview_path = os.path.join(temp_dir, "output_preview.mp4")
                self._concat_videos(segment_videos_preview, output_preview_path, transparent=False)
                temp_files.append(output_preview_path)

                with open(output_transparent_path, 'rb') as f:
                    video_transparent = f.read()
                with open(output_preview_path, 'rb') as f:
                    video_preview = f.read()

                print("動画生成完了！（透過 + プレビュー）")
                return (video_transparent, video_preview)
            else:
                # 非透過動画（MP4のみ）
                output_path = os.path.join(temp_dir, "output.mp4")
                self._concat_videos(segment_videos_transparent, output_path, transparent=False)
                temp_files.append(output_path)

                with open(output_path, 'rb') as f:
                    video_data = f.read()

                print("動画生成完了！")
                return (video_data, None)

        finally:
            # クリーンアップ
            for f in temp_files:
                if os.path.exists(f):
                    os.unlink(f)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)

    def _get_audio_duration(self, audio_path: str) -> float:
        """音声ファイルの長さを取得（ffprobe優先、なければffmpegで取得）"""
        if FFPROBE_BIN:
            result = subprocess.run(
                [FFPROBE_BIN, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
                capture_output=True, text=True
            )
            return float(result.stdout.strip())
        else:
            result = subprocess.run(
                [FFMPEG_BIN, '-i', audio_path, '-f', 'null', '-'],
                capture_output=True, text=True
            )
            match = re.search(r'Duration:\s*(\d+):(\d+):(\d+)\.(\d+)', result.stderr)
            if match:
                h, m, s, cs = match.groups()
                return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100
            raise RuntimeError(f"音声ファイルの長さを取得できませんでした: {audio_path}")

    def _create_video_segment(self, img_path: str, audio_path: str, output_path: str, duration: float, fps: int, transparent: bool = False):
        """1つのセグメント動画を作成（音声と映像を同期）"""
        if transparent:
            # ProRes 4444（アルファチャンネル対応）
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-loop', '1',
                '-framerate', str(fps),
                '-t', str(duration),
                '-i', img_path,
                '-i', audio_path,
                '-c:v', 'prores_ks',
                '-profile:v', '4444',
                '-pix_fmt', 'yuva444p10le',
                '-c:a', 'pcm_s16le',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-vsync', 'cfr',
                output_path
            ], capture_output=True, check=True)
        else:
            # 通常のMP4
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-loop', '1',
                '-framerate', str(fps),
                '-t', str(duration),
                '-i', img_path,
                '-i', audio_path,
                '-c:v', 'libx264',
                '-tune', 'stillimage',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-pix_fmt', 'yuv420p',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-vsync', 'cfr',
                output_path
            ], capture_output=True, check=True)

    def _concat_videos(self, video_paths: list, output_path: str, transparent: bool = False):
        """複数の動画を連結（音声同期を維持）"""
        # 連結リストファイルを作成
        list_path = output_path + '.txt'
        with open(list_path, 'w') as f:
            for vp in video_paths:
                f.write(f"file '{vp}'\n")

        if transparent:
            # ProRes 4444を維持（音声同期オプション付き）
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_path,
                '-c:v', 'prores_ks',
                '-profile:v', '4444',
                '-pix_fmt', 'yuva444p10le',
                '-c:a', 'pcm_s16le',
                '-vsync', 'cfr',
                '-af', 'aresample=async=1',
                output_path
            ], capture_output=True, check=True)
        else:
            # MP4（再エンコードで同期を確保）
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '18',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-vsync', 'cfr',
                '-af', 'aresample=async=1',
                output_path
            ], capture_output=True, check=True)

        os.unlink(list_path)

    def create_video_with_audio_segments(
        self,
        display_text: str,
        audio_segments: list,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        transparent: bool = False,
        progress_callback=None
    ) -> tuple:
        """既に生成された音声セグメントを使用して動画を生成

        Args:
            display_text: 表示用テキスト（改行区切り）
            audio_segments: 行ごとの音声データ（bytesのリスト）
            progress_callback: 進捗コールバック関数 (current, total, message) を受け取る

        Returns:
            tuple: (透過動画bytes, プレビュー動画bytes) - 透過モード時
                   (動画bytes, None) - 非透過モード時
        """
        # 表示用テキストを行ごとに分割し、句読点を削除
        display_lines = []
        for line in display_text.strip().split('\n'):
            line = line.strip()
            if line:
                display_lines.append(remove_punctuation_for_display(line))

        # 行数と音声セグメント数が一致するか確認
        if len(display_lines) != len(audio_segments):
            print(f"警告: 表示用テキスト({len(display_lines)}行)と音声セグメント({len(audio_segments)}個)の数が異なります")
            min_len = min(len(display_lines), len(audio_segments))
            display_lines = display_lines[:min_len]
            audio_segments = audio_segments[:min_len]

        if not display_lines:
            raise ValueError("テキストが空です")

        total_clips = len(display_lines)

        temp_dir = tempfile.mkdtemp()
        temp_files = []
        segment_videos_transparent = []
        segment_videos_preview = []

        try:
            # 各行の動画セグメントを作成
            for i, (display_line, audio_data) in enumerate(zip(display_lines, audio_segments)):
                clip_num = i + 1
                print(f"セグメント {clip_num}/{total_clips} を作成中: {display_line[:20]}...")

                # 進捗コールバック
                if progress_callback:
                    progress_callback(clip_num, total_clips, f"クリップ {clip_num}/{total_clips} を生成中...")

                # 1. 音声を一時ファイルに保存
                audio_path = os.path.join(temp_dir, f"audio_{i}.wav")
                with open(audio_path, 'wb') as f:
                    f.write(audio_data)
                temp_files.append(audio_path)

                # 2. 音声の長さを取得
                duration = self._get_audio_duration(audio_path)

                if transparent:
                    # 透過モード：透過画像とチェッカー画像の両方を生成
                    # 透過画像
                    img_transparent = self._create_text_image(display_line, width, height, transparent=True)
                    img_transparent_path = os.path.join(temp_dir, f"frame_transparent_{i}.png")
                    img_transparent.save(img_transparent_path)
                    temp_files.append(img_transparent_path)

                    # チェッカー背景画像（プレビュー用）
                    img_preview = self._create_text_image(display_line, width, height, checker=True)
                    img_preview_path = os.path.join(temp_dir, f"frame_preview_{i}.png")
                    img_preview.save(img_preview_path)
                    temp_files.append(img_preview_path)

                    # 透過動画セグメント
                    video_transparent_path = os.path.join(temp_dir, f"segment_transparent_{i}.mov")
                    self._create_video_segment(img_transparent_path, audio_path, video_transparent_path, duration, fps, transparent=True)
                    segment_videos_transparent.append(video_transparent_path)
                    temp_files.append(video_transparent_path)

                    # プレビュー動画セグメント（MP4）
                    video_preview_path = os.path.join(temp_dir, f"segment_preview_{i}.mp4")
                    self._create_video_segment(img_preview_path, audio_path, video_preview_path, duration, fps, transparent=False)
                    segment_videos_preview.append(video_preview_path)
                    temp_files.append(video_preview_path)
                else:
                    # 非透過モード：グリーンバック動画のみ
                    img = self._create_text_image(display_line, width, height, transparent=False)
                    img_path = os.path.join(temp_dir, f"frame_{i}.png")
                    img.save(img_path)
                    temp_files.append(img_path)

                    video_path = os.path.join(temp_dir, f"segment_{i}.mp4")
                    self._create_video_segment(img_path, audio_path, video_path, duration, fps, transparent=False)
                    segment_videos_transparent.append(video_path)
                    temp_files.append(video_path)

            # 全セグメントを連結
            print(f"全 {len(segment_videos_transparent)} セグメントを連結中...")

            if transparent:
                # 透過動画（MOV）
                output_transparent_path = os.path.join(temp_dir, "output_transparent.mov")
                self._concat_videos(segment_videos_transparent, output_transparent_path, transparent=True)
                temp_files.append(output_transparent_path)

                # プレビュー動画（MP4）
                output_preview_path = os.path.join(temp_dir, "output_preview.mp4")
                self._concat_videos(segment_videos_preview, output_preview_path, transparent=False)
                temp_files.append(output_preview_path)

                with open(output_transparent_path, 'rb') as f:
                    video_transparent = f.read()
                with open(output_preview_path, 'rb') as f:
                    video_preview = f.read()

                print("動画生成完了！（透過 + プレビュー）")
                return (video_transparent, video_preview)
            else:
                # 非透過動画（MP4のみ）
                output_path = os.path.join(temp_dir, "output.mp4")
                self._concat_videos(segment_videos_transparent, output_path, transparent=False)
                temp_files.append(output_path)

                with open(output_path, 'rb') as f:
                    video_data = f.read()

                print("動画生成完了！")
                return (video_data, None)

        finally:
            # クリーンアップ
            for f in temp_files:
                if os.path.exists(f):
                    os.unlink(f)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)

    def create_video_from_timestamped_segments(
        self,
        audio_path: str,
        segments: list,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        transparent: bool = True,
        progress_callback=None,
        audio_margin: float = 0.0
    ) -> tuple:
        """タイムスタンプ付きセグメントから動画を生成（音声を切らない方式）

        Args:
            audio_path: 元の音声ファイルパス
            segments: [{"start": 0.0, "end": 1.5, "text": "テキスト"}, ...]
            progress_callback: 進捗コールバック関数
            audio_margin: 未使用（互換性のため残す）

        Returns:
            tuple: (透過動画bytes, プレビュー動画bytes)
        """
        if not segments:
            raise ValueError("セグメントがありません")

        total_clips = len(segments)
        total_audio_duration = self._get_audio_duration(audio_path)
        frame_duration = 1.0 / fps

        print(f"[VIDEO TIMING] Audio duration: {total_audio_duration:.4f}s, FPS: {fps}, Frame duration: {frame_duration:.6f}s")

        temp_dir = tempfile.mkdtemp()

        try:
            # 1. 各セグメントの表示時間を正確に計算
            #    FFmpeg concat demuxerで一括処理するため、個別エンコード時の
            #    フレーム境界丸め誤差の蓄積が発生しない

            # FIX 3: リードイン（音声冒頭の無音区間）を最初のテロップに吸収
            raw_lead_in = segments[0]["start"]
            lead_in_frames = round(raw_lead_in / frame_duration)
            lead_in_duration = 0  # 空白フレームなし（最初のテロップを即表示）
            print(f"[VIDEO TIMING] Lead-in: raw={raw_lead_in:.4f}s, frames={lead_in_frames}, absorbed into first segment")

            durations = []
            for i in range(total_clips):
                # 最初のセグメントはtime=0から開始（リードイン吸収）
                start_time = 0 if i == 0 else segments[i]["start"]

                if i < total_clips - 1:
                    end_time = segments[i + 1]["start"]
                else:
                    end_time = total_audio_duration

                raw_duration = end_time - start_time

                # FIX 2: durationをフレーム境界に丸める
                num_frames = max(1, round(raw_duration / frame_duration))
                duration = num_frames * frame_duration

                durations.append(duration)

            # FIX 5: 映像合計時間を音声に一致させる（最終セグメントで調整）
            total_video_duration = lead_in_duration + sum(durations)
            duration_diff = total_audio_duration - total_video_duration
            if abs(duration_diff) > 0.001 and durations:
                adjusted = durations[-1] + duration_diff
                if adjusted >= frame_duration:
                    print(f"[VIDEO TIMING] Adjusting last segment: {durations[-1]:.6f}s -> {adjusted:.6f}s (diff={duration_diff:+.6f}s)")
                    durations[-1] = adjusted

            total_video_duration = lead_in_duration + sum(durations)
            print(f"[VIDEO TIMING] Total video: {total_video_duration:.4f}s, Total audio: {total_audio_duration:.4f}s, Diff: {total_video_duration - total_audio_duration:.6f}s")

            # 2. 全テロップ画像を生成
            img_transparent_paths = []
            img_preview_paths = []
            img_green_paths = []

            for i, seg in enumerate(segments):
                clip_num = i + 1
                text = seg["text"].strip()
                display_text = remove_punctuation_for_display(text)

                print(f"セグメント {clip_num}/{total_clips}: {display_text[:20]}... (duration={durations[i]:.3f}s)")

                if progress_callback:
                    progress_callback(clip_num, total_clips, f"クリップ {clip_num}/{total_clips} を生成中...")

                if transparent:
                    img_t = self._create_text_image(display_text, width, height, transparent=True)
                    path_t = os.path.join(temp_dir, f"frame_t_{i}.png")
                    img_t.save(path_t)
                    img_transparent_paths.append(path_t)

                    img_p = self._create_text_image(display_text, width, height, checker=True)
                    path_p = os.path.join(temp_dir, f"frame_p_{i}.png")
                    img_p.save(path_p)
                    img_preview_paths.append(path_p)
                else:
                    img = self._create_text_image(display_text, width, height, transparent=False)
                    path = os.path.join(temp_dir, f"frame_{i}.png")
                    img.save(path)
                    img_green_paths.append(path)

            # 3. リードイン用の空白フレームを生成（音声冒頭の無音区間）
            lead_in_t_entries = []
            lead_in_p_entries = []
            lead_in_g_entries = []
            if lead_in_duration > 0:
                print(f"リードイン: {lead_in_duration:.3f}s の空白フレームを追加")
                if transparent:
                    blank_t = self._create_text_image("", width, height, transparent=True)
                    blank_t_path = os.path.join(temp_dir, "frame_t_leadin.png")
                    blank_t.save(blank_t_path)
                    lead_in_t_entries = [(blank_t_path, lead_in_duration)]

                    blank_p = self._create_text_image("", width, height, checker=True)
                    blank_p_path = os.path.join(temp_dir, "frame_p_leadin.png")
                    blank_p.save(blank_p_path)
                    lead_in_p_entries = [(blank_p_path, lead_in_duration)]
                else:
                    blank_g = self._create_text_image("", width, height, transparent=False)
                    blank_g_path = os.path.join(temp_dir, "frame_leadin.png")
                    blank_g.save(blank_g_path)
                    lead_in_g_entries = [(blank_g_path, lead_in_duration)]

            # 4. 画像+duration一覧から映像を1パスで生成（タイミング精度向上）
            print(f"全 {total_clips} セグメントの映像を一括生成中...")

            if transparent:
                video_t_path = os.path.join(temp_dir, "video_transparent.mov")
                self._create_video_from_images_concat(
                    lead_in_t_entries + list(zip(img_transparent_paths, durations)),
                    video_t_path, fps, transparent=True)

                video_p_path = os.path.join(temp_dir, "video_preview.mp4")
                self._create_video_from_images_concat(
                    lead_in_p_entries + list(zip(img_preview_paths, durations)),
                    video_p_path, fps, transparent=False)

                # 元の音声と結合
                output_t = os.path.join(temp_dir, "output_transparent.mov")
                self._mux_video_audio(video_t_path, audio_path, output_t, transparent=True)

                output_p = os.path.join(temp_dir, "output_preview.mp4")
                self._mux_video_audio(video_p_path, audio_path, output_p, transparent=False)

                with open(output_t, 'rb') as f:
                    video_transparent = f.read()
                with open(output_p, 'rb') as f:
                    video_preview = f.read()

                print("動画生成完了！（透過 + プレビュー）")
                return (video_transparent, video_preview)
            else:
                video_path = os.path.join(temp_dir, "video.mp4")
                self._create_video_from_images_concat(
                    lead_in_g_entries + list(zip(img_green_paths, durations)),
                    video_path, fps, transparent=False)

                output_path = os.path.join(temp_dir, "output.mp4")
                self._mux_video_audio(video_path, audio_path, output_path, transparent=False)

                with open(output_path, 'rb') as f:
                    video_data = f.read()

                print("動画生成完了！")
                return (video_data, None)

        finally:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_audio_segment(self, input_path: str, output_path: str, start_time: float, duration: float):
        """音声ファイルから指定区間を切り出し"""
        subprocess.run([
            FFMPEG_BIN, '-y',
            '-i', input_path,
            '-ss', str(start_time),
            '-t', str(duration),
            '-c:a', 'pcm_s16le',
            '-ar', '44100',
            '-ac', '2',
            output_path
        ], capture_output=True, check=True)

    def _create_video_only_segment(self, img_path: str, output_path: str, duration: float, fps: int, transparent: bool = False):
        """音声なしの映像セグメントを作成"""
        if transparent:
            # ProRes 4444（アルファチャンネル対応）
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-loop', '1',
                '-framerate', str(fps),
                '-i', img_path,
                '-t', str(duration),
                '-c:v', 'prores_ks',
                '-profile:v', '4444',
                '-pix_fmt', 'yuva444p10le',
                '-an',
                output_path
            ], capture_output=True, check=True)
        else:
            # 通常のMP4（音声なし）
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-loop', '1',
                '-framerate', str(fps),
                '-i', img_path,
                '-t', str(duration),
                '-c:v', 'libx264',
                '-tune', 'stillimage',
                '-pix_fmt', 'yuv420p',
                '-an',
                output_path
            ], capture_output=True, check=True)

    def _concat_videos_no_audio(self, video_paths: list, output_path: str, transparent: bool = False):
        """音声なしの動画を連結"""
        list_path = output_path + '.txt'
        with open(list_path, 'w') as f:
            for vp in video_paths:
                f.write(f"file '{vp}'\n")

        if transparent:
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_path,
                '-c:v', 'prores_ks',
                '-profile:v', '4444',
                '-pix_fmt', 'yuva444p10le',
                '-an',
                output_path
            ], capture_output=True, check=True)
        else:
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '18',
                '-an',
                output_path
            ], capture_output=True, check=True)

        os.unlink(list_path)

    def _mux_video_audio(self, video_path: str, audio_path: str, output_path: str, transparent: bool = False):
        """映像と音声を結合（元の音声をそのまま使用）"""
        if transparent:
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',
                '-c:a', 'pcm_s16le',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-shortest',
                output_path
            ], capture_output=True, check=True)
        else:
            subprocess.run([
                FFMPEG_BIN, '-y',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-shortest',
                output_path
            ], capture_output=True, check=True)

    def _create_video_from_images_concat(self, entries, output_path, fps=30, transparent=False):
        """画像リストとdurationから映像を1パスで生成（タイミング精度向上）

        個別にセグメント動画を作成→結合する方式と異なり、
        FFmpeg concatデマルチプレクサで一括処理することで
        フレーム境界の丸め誤差の蓄積を防ぎます。

        Args:
            entries: [(image_path, duration), ...] のリスト
            output_path: 出力動画パス
            fps: フレームレート
            transparent: ProRes 4444（透過）で出力するか
        """
        list_path = output_path + '_concat.txt'
        with open(list_path, 'w') as f:
            for img_path, duration in entries:
                f.write(f"file '{img_path}'\n")
                f.write(f"duration {duration:.6f}\n")
            # 最後のエントリを重複させて最終フレームを確実に表示
            if entries:
                f.write(f"file '{entries[-1][0]}'\n")

        try:
            if transparent:
                subprocess.run([
                    FFMPEG_BIN, '-y',
                    '-f', 'concat', '-safe', '0',
                    '-i', list_path,
                    '-vsync', 'cfr',
                    '-r', str(fps),
                    '-c:v', 'prores_ks',
                    '-profile:v', '4444',
                    '-pix_fmt', 'yuva444p10le',
                    '-an',
                    output_path
                ], capture_output=True, check=True)
            else:
                subprocess.run([
                    FFMPEG_BIN, '-y',
                    '-f', 'concat', '-safe', '0',
                    '-i', list_path,
                    '-vsync', 'cfr',
                    '-r', str(fps),
                    '-c:v', 'libx264',
                    '-tune', 'stillimage',
                    '-pix_fmt', 'yuv420p',
                    '-an',
                    output_path
                ], capture_output=True, check=True)
        finally:
            if os.path.exists(list_path):
                os.unlink(list_path)
