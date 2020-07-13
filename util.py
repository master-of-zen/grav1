import contextlib, os
from tempfile import NamedTemporaryFile

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
