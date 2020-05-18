#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, json
from tempfile import NamedTemporaryFile
from threading import Lock, Thread

def print_progress(n, total, size=10, suffix=""):
  fill = "█" * int((n / total) * size)
  remaining = " " * (size - len(fill))
  return f"{int(100 * n / total):3d}%|{fill}{remaining}| {n}/{total}"

def get_frames(input):
  cmd = f"ffmpeg -hide_banner -map 0:v:0 -c copy -f null {os.devnull} -i".split(" ")
  cmd.append(input)
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  match = re.search(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8"))
  if match:
    return int(match.group(1))
  else:
    return None

@contextlib.contextmanager
def tmp_file(mode, stream, suffix, cb):
  try:
    file = NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    tmp_name = file.name
    downloaded = 0
    total_size = int(stream.headers["content-length"])
    for chunk in stream.iter_content(chunk_size=8192):
      if chunk:
        downloaded = downloaded + len(chunk)
        cb(f"downloading {print_progress(downloaded, total_size)}")
        file.write(chunk)
    file.flush()
    file.close()
    yield tmp_name
  finally:
    os.unlink(tmp_name)

def vp9_encode(worker, input, encoder_params, args, status_cb):
  output_filename = f"{input}.webm"

  vp9 = f"ffmpeg -y -hide_banner".split(" ")
  vp9.extend(["-i",  input, "-c:v", "libvpx-vp9", "-an", "-passlogfile", f"{input}.log"])
  vp9.extend(encoder_params.split(" "))
  passes = [vp9 + cmd for cmd in [
    ["-pass", "1", "-f", "webm", os.devnull],
    ["-pass", "2", output_filename]
  ]]

  total_frames = get_frames(input)
  if total_frames is None: return False

  success = True
  for pass_n, cmd in enumerate(passes, start=1):
    worker.pipe = subprocess.Popen(cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      universal_newlines=True)

    status_cb(f"vp9 pass: {pass_n} {print_progress(0, total_frames)}")

    while True:
      line = worker.pipe.stdout.readline().strip()

      if len(line) == 0 and worker.pipe.poll() is not None:
        break

      matches = re.findall(r"frame= *([^ ]+?) ", line)
      if matches:
        status_cb(f"vp9 pass: {pass_n} {print_progress(int(matches[-1]), total_frames)}")
    
    if worker.pipe.returncode != 0:
      status_cb("error")
      success = False

  if os.path.isfile(f"{input}.log-0.log"):
    os.remove(f"{input}.log-0.log")

  if success:
    return output_filename
  else:
    return False

def aom_encode(worker, input, encoder_params, status_cb):
  if "vmaf" in encoder_params and len(worker.client.args.vmaf_path) > 0:
    encoder_params = f"{encoder_params} --vmaf-model-path={worker.client.args.vmaf_path}"

  output_filename = f"{input}.ivf"

  ffmpeg = f"ffmpeg -y -hide_banner -loglevel error".split(" ")
  ffmpeg.extend(["-i",  input])
  ffmpeg.extend("-strict -1 -pix_fmt yuv420p -f yuv4mpegpipe -".split(" "))

  aom = f"aomenc - --fpf={input}.log --threads={args.threads} {encoder_params}".split(" ")

  aom.append("--passes=2")
  passes = [aom + cmd for cmd in [
    ["--pass=1", "-o", os.devnull],
    ["--pass=2", "-o", output_filename]
  ]]

  total_frames = get_frames(input)
  if total_frames is None: return False

  success = True
  for pass_n, cmd in enumerate(passes, start=1):
    ffmpeg_pipe = subprocess.Popen(ffmpeg,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT)

    worker.pipe = subprocess.Popen(cmd,
      stdin=ffmpeg_pipe.stdout,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      universal_newlines=True)

    status_cb(f"aom pass: {pass_n} {print_progress(0, total_frames)}")

    while True:
      line = worker.pipe.stdout.readline().strip()

      if len(line) == 0 and worker.pipe.poll() is not None:
        break

      match = re.search(r"frame.*?\/([^ ]+?) ", line)
      if match:
        status_cb(f"aom pass: {pass_n} {print_progress(int(match.group(1)), total_frames)}")
    
    if worker.pipe.returncode != 0:
      status_cb("error")
      success = False

  if os.path.isfile(f"{input}.log"):
    os.remove(f"{input}.log")

  if success:
    return output_filename
  else:
    return False

def fetch_new_job(client):
  jobs_str = json.dumps([{"projectid": job.projectid, "scene": job.scene} for job in client.jobs])
  try:
    r = client.session.get(f"{client.args.target}/api/get_job/{jobs_str}", timeout=3, stream=True)
    if r.status_code != 200 or "success" not in r.headers or r.headers["success"] == "0":
      return None

    job = type("", (), {})
    job.id = r.headers["id"]
    job.filename = r.headers["filename"]
    job.scene = r.headers["scene"]
    job.encoder = r.headers["encoder"]
    job.encoder_params = r.headers["encoder_params"]
    job.projectid = r.headers["projectid"]
    job.request = r
    client.jobs.append(job)

    return job
  except:
    return None

def upload(client, job, file, output):
  files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
  try:
    r = client.session.post(
      client.args.target + "/finish_job",
      data={
        "id": job.id,
        "scene": job.scene,
        "projectid": job.projectid,
        "encoder": job.encoder,
        "encoder_params": job.encoder_params
      },
      files=files)
    return r
  except:
    return False

class Client:
  def __init__(self, args):
    self.args = args
    self.workers = []
    self.numworkers = int(args.workers)
    self.completed = 0
    self.failed = 0
    self.jobs = []
    self.lock = Lock()
    self.session = requests.Session()

class Worker:
  def __init__(self, client):
    self.status = ""
    self.client = client
    self.lock_aquired = False
    self.thread = None
    self.job = None
    self.pipe = None
    self.stopped = False

  def kill(self):
    self.stopped = True
    if self.pipe:
      self.pipe.kill()

  def start(self):
    self.thread = Thread(target=lambda: self.work(), daemon=True)
    self.thread.start()

  def update_status(self, status):
    if self.client.args.noui:
      print(status)
    self.status = status

  def work(self):
    while True:
      self.update_status("waiting")

      if self.job is not None and self.job in self.client.jobs:
        self.client.jobs.remove(self.job)

      if not self.lock_aquired:
        self.client.lock.acquire()
        self.lock_aquired = True

      self.update_status("downloading")

      while True:
        self.job = fetch_new_job(self.client)
        if self.job: break
        for i in range(0, 15):
          if self.stopped: return
          self.update_status(f"waiting...{15-i:2d}")
          time.sleep(1)

      self.client.lock.release()
      self.lock_aquired = False

      with tmp_file("wb", self.job.request, self.job.filename, self.update_status) as file:
        if self.job.encoder == "vp9":
          output = vp9_encode(self, file, self.job.encoder_params, self.update_status)
        elif self.job.encoder == "aom":
          output = aom_encode(self, file, self.job.encoder_params, self.update_status)
        else: return

        if output:
          self.update_status(f"uploading {self.job.projectid} {self.job.scene}")
          with open(output, "rb") as file:
            while True:
              r = upload(self.client, self.job, file, output)
              if r:
                if r.text == "saved":
                  self.client.completed += 1
                else:
                  self.client.failed += 1
                  self.update_status(f"error {r.status_code}")
                  time.sleep(1)
                break
              else:
                self.update_status("unable to connect - trying again")
                time.sleep(1)

          while os.path.isfile(output):
            try:
              os.remove(output)
            except:
              time.sleep(1)

          self.client.jobs.remove(self.job)
          self.job = None

KEY_UP = 259
KEY_DOWN = 258
KEY_LEFT = 260
KEY_RIGHT = 261
KEY_RETURN = 10

def window(scr):
  curses.curs_set(0)
  scr.nodelay(1)

  menu = type("", (), {})
  menu.selected_item = 0
  menu.items = ["add", "remove", "remove (f)", "quit"]
  menu.scroll = 0
  
  curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)

  while True:
    (mlines, mcols) = scr.getmaxyx()
    y = 0

    scr.erase()

    for line in textwrap.wrap(f"target: {args.target} workers: {args.workers} hit: {client.completed} miss: {client.failed}", width=mcols):
      scr.insstr(y, 0, line.ljust(mcols), curses.color_pair(1))
      y += 1

    msg = []
    for i, worker in enumerate(client.workers, start=1):
      msg.append(f"{i:2} {worker.status}")

    for i, line in enumerate(msg[menu.scroll:mlines - y - 1 + menu.scroll]):
      scr.insstr(y + i, 0, line)

    line = " ".join([f"[{item}]" if i == menu.selected_item else f" {item} " for i, item in enumerate(menu.items)])
    scr.insstr(mlines - 1, 0, line.ljust(mcols), curses.color_pair(1))
    
    scr.refresh()

    c = scr.getch()

    if c == KEY_UP:
      menu.scroll = max(menu.scroll - 1, 0)
    elif c == KEY_DOWN:
      menu.scroll = min(menu.scroll + 1, len(client.workers) - (mlines - y - 1))
    elif c == KEY_LEFT:
      menu.selected_item = max(menu.selected_item - 1, 0)
    elif c == KEY_RIGHT:
      menu.selected_item = min(menu.selected_item + 1, len(menu.items) - 1)
    elif c == KEY_RETURN:
      if menu.selected_item == 0:
        pass
      elif menu.selected_item == 1:
        pass
      elif menu.selected_item == 2:
        pass
      elif menu.selected_item == 3:
        for worker in client.workers:
          worker.kill()
        break
  
  curses.curs_set(1)

windows_binaries = [
  ("vmaf_v0.6.1.pkl", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl", "binary"),
  ("vmaf_v0.6.1.pkl.model", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl.model", "binary"),
  ("ffmpeg.exe", "https://f.grass.moe/f/Sy/ffmpeg.zip", "zip")
]

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("target", type=str, nargs="?", default="https://encode.grass.moe/1")
  parser.add_argument("--vmaf-model-path", dest="vmaf_path", default="vmaf_v0.6.1.pkl" if os.name == "nt" else "")
  parser.add_argument("--workers", dest="workers", default=1)
  parser.add_argument("--threads", dest="threads", default=4)
  parser.add_argument("--noui", action="store_const", const=True)

  args = parser.parse_args()
  
  if os.name == "nt":
    if not os.path.isfile("aomenc.exe"):
      with requests.get("https://ci.appveyor.com/api/projects/Randomderp/aom") as r:
        latest_job = r.json()["build"]["jobs"][0]["jobId"]
        windows_binaries.append(("aomenc.exe", f"https://ci.appveyor.com/api/buildjobs/{latest_job}/artifacts/aom.zip", "zip"))
    
    for file in windows_binaries:
      if not os.path.isfile(file[0]):
        print(file[0], "is missing, downloading...")

        r = requests.get(file[1])

        if file[2] == "binary":
          with open(file[0], "wb+") as f:
            f.write(r.content)

        if file[2] == "zip":
          print("unpacking")
          from zipfile import ZipFile
          from io import BytesIO
          zipdata = BytesIO()
          zipdata.write(r.content)
          zipfile = ZipFile(zipdata)
          with zipfile.open(file[0]) as file_in_zip:
            with open(file[0], "wb+") as f:
              f.write(file_in_zip.read())

  client = Client(args)

  for i in range(0, int(args.workers)):
    client.workers.append(Worker(client))
  
  for worker in client.workers:
    worker.start()

  if args.noui:
    for worker in client.workers:
      worker.thread.join()
  else:
    import curses, textwrap
    curses.wrapper(window)
