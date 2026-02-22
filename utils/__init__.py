# Utils package
import re

# 統一された句読点・空白パターン（タイミング計算・テロップ表示共通）
_PUNCTUATION_PATTERN = re.compile(r'[、。,.\s　，．！？!?]')


def normalize_for_timing(text: str) -> str:
    """タイミング計算用: 句読点・空白を全て除去して文字数カウントに使う"""
    return _PUNCTUATION_PATTERN.sub('', text)


def remove_punctuation_for_display(text: str) -> str:
    """テロップ表示用: 句読点を除去（同じパターンで統一）"""
    return _PUNCTUATION_PATTERN.sub('', text)
