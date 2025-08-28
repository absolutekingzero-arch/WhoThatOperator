# bot/__init__.py
"""Lightweight package init — lazy import to avoid circular imports."""
__all__ = ['bot']

def get_bot():
    # import thực hiện chỉ khi được gọi
    from .bot import bot as _bot
    return _bot
