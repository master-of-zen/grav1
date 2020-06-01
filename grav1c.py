#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, json
from tempfile import NamedTemporaryFile
from threading import Lock, Thread, Event

bytes_map = ["B", "K", "M", "G"]

KEY_UP = 259
KEY_DOWN = 258
KEY_LEFT = 260
KEY_RIGHT = 261
KEY_RETURN = 10

def n_bytes(num_bytes):
  if num_bytes / 1024 < 1: return (num_bytes, 0)
  r = n_bytes(num_bytes / 1024)
  return (r[0], r[1] + 1)

def bytes_str(num_bytes):
  r = n_bytes(num_bytes)
  return f"{r[0]:.1f}{bytes_map[r[1]]}"

def print_progress_bytes(n, total):
  fill = "█" * int((n / total) * 10)
  return "{:3.0f}%|{:{}s}| {}/{}".format(100 * n / total, fill, 10, bytes_str(n), bytes_str(total))

def print_progress(n, total):
  fill = "█" * int((n / total) * 10)
  return "{:3.0f}%|{:{}s}| {}/{}".format(100 * n / total, fill, 10, n, total)

def get_frames(input):
  cmd = ["ffmpeg", "-hide_banner", "-i", input, "-map", "0:v:0", "-c", "copy", "-f", "null", "-"]
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  matches = re.findall(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8"))
  if matches:
    return int(matches[-1])
  else:
    return None

@contextlib.contextmanager
def tmp_file(mode, stream, suffix, cb):
  try:
    file = NamedTemporaryFile(mode=mode, suffix=suffix, dir=".", delete=False)
    tmp_name = file.name
    downloaded = 0
    total_size = int(stream.headers["content-length"])
    for chunk in stream.iter_content(chunk_size=2**14):
      if chunk:
        downloaded = downloaded + len(chunk)
        cb(f"downloading {print_progress_bytes(downloaded, total_size)}")
        file.write(chunk)
    file.flush()
    file.close()
    yield tmp_name
  finally:
    while os.path.exists(tmp_name):
      try:
        os.unlink(tmp_name)
      except:
        pass

def aom_vpx_encode(worker, encoder, input, encoder_params, status_cb):
  if encoder == "aomenc":
    if "vmaf" in encoder_params and len(worker.client.args.vmaf_path) > 0:
      encoder_params = f"{encoder_params} --vmaf-model-path={worker.client.args.vmaf_path}"

  output_filename = f"{input}.ivf"

  ffmpeg = f"ffmpeg -y -hide_banner -loglevel error".split(" ")
  ffmpeg.extend(["-i",  input])
  ffmpeg.extend("-strict -1 -pix_fmt yuv420p -f yuv4mpegpipe -".split(" "))

  aom = [encoder, "-", "--ivf", f"--fpf={input}.log", f"--threads={args.threads}", "--passes=2"]

  passes = [aom + cmd for cmd in [
    re.sub(r"--denoise-noise-level=[0-9]+", "", encoder_params).split(" ") + ["--pass=1", "-o", os.devnull],
    encoder_params.split(" ") + ["--pass=2", "-o", output_filename]
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

    worker.progress = (pass_n, 0)
    status_cb(f"{encoder:.3s} pass: {pass_n} {print_progress(0, total_frames)}")

    while True:
      line = worker.pipe.stdout.readline().strip()

      if len(line) == 0 and worker.pipe.poll() is not None:
        break

      match = re.search(r"frame.*?\/([^ ]+?) ", line)
      if match:
        worker.progress = (pass_n, int(match.group(1)))
        status_cb(f"{encoder:.3s} pass: {pass_n} {print_progress(int(match.group(1)), total_frames)}")
    
    if worker.pipe.returncode != 0:
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
    self.scr = None
    
    self.menu = type("", (), {})
    self.menu.selected_item = 0
    self.menu.items = ["add", "remove", "remove (f)", "quit"]
    self.menu.scroll = 0
    self.refreshing = False
    self.screen_thread = Thread(target=self.screen, daemon=True)
    self.refresh = Event()
    self.screen_thread.start()

  def screen(self):
    while self.refresh.wait():
      if not self.scr: continue
      msg = []
      for i, worker in enumerate(self.workers, start=1):
        msg.append(f"{i:2} {worker.status}")

      self.scr.erase()

      (mlines, mcols) = self.scr.getmaxyx()

      header = []
      for line in textwrap.wrap(f"target: {args.target} workers: {client.numworkers} hit: {client.completed} miss: {client.failed}", width=mcols):
        header.append(line)

      body_y = len(header)
      window_size = mlines - body_y - 1
      self.menu.scroll = max(min(self.menu.scroll, len(client.workers) - window_size), 0)

      for i, line in enumerate(header):
        self.scr.insstr(i, 0, line.ljust(mcols), curses.color_pair(1))

      for i, line in enumerate(msg[self.menu.scroll:window_size + self.menu.scroll], start=body_y):
        self.scr.insstr(i, 0, line)

      footer = " ".join([f"[{item}]" if i == self.menu.selected_item else f" {item} " for i, item in enumerate(self.menu.items)])
      self.scr.insstr(mlines - 1, 0, footer.ljust(mcols), curses.color_pair(1))
      
      self.scr.refresh()
      self.refresh.clear()

  def refresh_screen(self):
    self.refresh.set()
  
  def window(self, scr):
    self.scr = scr

    curses.curs_set(0)
    scr.nodelay(0)

    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)

    self.refresh_screen()
    
    while True:
      c = scr.getch()

      if c == KEY_UP:
        self.menu.scroll -= 1
      elif c == KEY_DOWN:
        self.menu.scroll += 1
      elif c == KEY_LEFT:
        self.menu.selected_item = max(self.menu.selected_item - 1, 0)
      elif c == KEY_RIGHT:
        self.menu.selected_item = min(self.menu.selected_item + 1, len(self.menu.items) - 1)
      elif c == KEY_RETURN:
        menu_action = self.menu.items[self.menu.selected_item]

        if menu_action == "add":
          self.numworkers += 1
          while len(self.workers) < self.numworkers:
            new_worker = Worker(self)
            self.workers.append(new_worker)
            new_worker.start()

        elif menu_action == "remove":
          self.numworkers = max(self.numworkers - 1, 0)

        elif menu_action == "remove (f)":
          if len(self.workers) == self.numworkers or any(worker for worker in self.workers if worker.job is None):
            self.numworkers = max(self.numworkers - 1, 0)

          if not any(worker for worker in self.workers if worker.pipe is None):
            sorted_workers = sorted([worker for worker in self.workers if not worker.stopped], key= lambda x: x.progress)
            if len(sorted_workers) > 0:
              sorted_workers[0].status = "killing"
              sorted_workers[0].kill()
          
        elif menu_action == "quit":
          for worker in self.workers:
            worker.kill()
          break

      self.refresh_screen()
        
    curses.curs_set(1)

class Worker:
  def __init__(self, client):
    self.status = ""
    self.client = client
    self.lock_aquired = False
    self.thread = None
    self.job = None
    self.pipe = None
    self.stopped = False
    self.progress = (0, 0)

  def kill(self):
    self.stopped = True
    if self.pipe:
      self.pipe.kill()

  def start(self):
    self.thread = Thread(target=lambda: self.work(), daemon=True)
    self.thread.start()

  def update_status(self, status):
    if self.stopped: return
    if self.client.args.noui:
      print(status)
    else:
      self.status = status
      self.client.refresh_screen()

  def work(self):
    while True:
      self.update_status("waiting")

      if self.job is not None and self.job in self.client.jobs:
        self.client.jobs.remove(self.job)

      if not self.lock_aquired:
        self.client.lock.acquire()
        self.lock_aquired = True

      if len(self.client.workers) > self.client.numworkers or self.stopped:
        self.client.lock.release()
        self.client.workers.remove(self)
        return

      self.update_status("downloading")

      while True:
        self.job = fetch_new_job(self.client)
        if self.job: break
        for i in range(0, 15):
          if self.stopped:
            self.client.lock.release()
            return
          self.update_status(f"waiting...{15-i:2d}")
          time.sleep(1)

      self.client.lock.release()
      self.lock_aquired = False

      with tmp_file("wb", self.job.request, self.job.filename, self.update_status) as file:
        if self.stopped: continue

        if self.job.encoder == "vpx":
          output = aom_vpx_encode(self, "vpxenc", file, self.job.encoder_params, self.update_status)
        elif self.job.encoder == "aom":
          output = aom_vpx_encode(self, "aomenc", file, self.job.encoder_params, self.update_status)
        else: continue

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

windows_binaries = [
  ("vmaf_v0.6.1.pkl", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl", "binary"),
  ("vmaf_v0.6.1.pkl.model", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl.model", "binary"),
  ("ffmpeg.exe", "https://www.sfu.ca/~ssleong/ffmpeg.zip", "zip"),
  ("vpxenc.exe", "https://www.sfu.ca/~ssleong/vpxenc.exe", "binary")
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
      with requests.get("https://ci.appveyor.com/api/projects/marcomsousa/build-aom") as r:
        latest_job = r.json()["build"]["jobs"][0]["jobId"]
        windows_binaries.append(("aomenc.exe", f"https://ci.appveyor.com/api/buildjobs/{latest_job}/artifacts/aomenc.exe", "binary"))
    
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
    curses.wrapper(lambda scr: client.window(scr))
