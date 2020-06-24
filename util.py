import subprocess, re, contextlib, os
from tempfile import NamedTemporaryFile

re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}).(\d{2})", re.U)
re_position = re.compile(r".*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", re.U)

def parse_time(search):
  search = re.match(r"[\x20-\x7E]+", search).group()
  return sum([float(t) * 60 ** i for i, t in enumerate(search.split(":")[::-1])])

def print_progress(n, total, size=20, suffix=""):
  return f"{int(100 * n / total):3d}% {n}/{total} {suffix}"

def get_frames(input, fast=True):
  cmd = ["ffmpeg", "-hide_banner", "-i", input, "-map", "0:v:0"]
  if fast:
    cmd.extend(["-c", "copy",])
  cmd.extend(["-f", "null", "-"])
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  matches = re.findall(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8"))
  return int(matches[-1])

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
  
def ffmpeg(cmd, cb):
  pipe = subprocess.Popen(cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    universal_newlines = True)

  try:
    while True:
      line = pipe.stdout.readline().strip()

      if len(line) == 0 and pipe.poll() is not None:
        break

      if not cb: continue
      matches = re.findall(r"frame= *([^ ]+?) ", line)
      if matches:
        cb(int(matches[-1]))

  except KeyboardInterrupt as e:
    pipe.kill()
    raise e
