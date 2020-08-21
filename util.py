import contextlib, os, tempfile

@contextlib.contextmanager
def tmp_file(mode, content, suffix=""):
  try:
    file = tempfile.NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    file.write(content)
    file.flush()
    tmp_name = file.name
    file.close()
    yield tmp_name
  finally:
    os.unlink(tmp_name)

@contextlib.contextmanager
def tmp_save(file, path, suffix=""):
  try:
    tmp_name = ""
    while not tmp_name or os.path.isfile(tmp_name):
      tmp_name = os.path.join(path, next(tempfile._get_candidate_names())) + suffix

    file.save(tmp_name)
    yield tmp_name
  finally:
    os.unlink(tmp_name)

