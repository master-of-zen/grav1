import re, contextlib, os
from tempfile import NamedTemporaryFile

re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}).(\d{2})", re.U)
re_position = re.compile(r".*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", re.U)

@contextlib.contextmanager
def tmp_file(mode, content, suffix=""):
  try:
    file = NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    file.write(content)
    file.flush()
    tmp_name = file.name
    file.close()
    yield tmp_name
  finally:
    os.unlink(tmp_name)
