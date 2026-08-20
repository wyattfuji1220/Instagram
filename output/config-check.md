# GitHub Actions 上での設定点検

実行: 2026-08-20 21:21 JST

```
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/home/runner/work/Instagram/Instagram/src/bookgram/__main__.py", line 1, in <module>
    from .cli import main
  File "/home/runner/work/Instagram/Instagram/src/bookgram/cli.py", line 40, in <module>
    from .generate import generate_book_post
  File "/home/runner/work/Instagram/Instagram/src/bookgram/generate.py", line 16, in <module>
    import anthropic
ModuleNotFoundError: No module named 'anthropic'
```
