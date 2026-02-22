import re

# タイミング計算用: 句読点・空白・括弧類を全て除去（音声に現れない文字を全て除く）
_TIMING_PATTERN = re.compile(r'[、。,.\s　，．！？!?【】「」『』（）()\[\]{}〈〉《》〔〕]')

# テロップ表示用: 句読点・空白のみ除去（括弧は残す）
_DISPLAY_PATTERN = re.compile(r'[、。,.\s　，．！？!?]')


def normalize_for_timing(text: str) -> str:
    """タイミング計算用: 句読点・空白・括弧類を全て除去して文字数カウントに使う"""
    return _TIMING_PATTERN.sub('', text)


def remove_punctuation_for_display(text: str) -> str:
    """テロップ表示用: 句読点・空白を除去（括弧は残す）"""
    return _DISPLAY_PATTERN.sub('', text)
