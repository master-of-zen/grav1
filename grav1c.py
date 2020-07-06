#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, json, shutil
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

def aom_vpx_encode(worker, encoder, video, job, status_cb):
  encoder_params = job.encoder_params
  ffmpeg_params = job.ffmpeg_params
  if encoder == "aomenc" and "vmaf" in encoder_params and len(worker.client.args.vmaf_path) > 0:
    encoder_params += f" --vmaf-model-path={worker.client.args.vmaf_path}"

  vfs = [f"select=gte(n\\,{job.start})"]

  vf_match = re.search(r"(?:-vf\s\"([^\"]+?)\"|-vf\s([^\s]+?)\s)", ffmpeg_params)

  if vf_match:
    vfs.append(vf_match.group(1) or vf_match.group(2))
    ffmpeg_params = re.sub(r"(?:-vf\s\"([^\"]+?)\"|-vf\s([^\s]+?)\s)", "", ffmpeg_params).strip()

  vf = ",".join(vfs)

  output_filename = f"{video}.ivf"

  ffmpeg = [
    "ffmpeg", "-y", "-hide_banner",
    "-loglevel", "error",
    "-i", video,
    "-strict", "-1",
    "-pix_fmt", "yuv420p",
    "-vf", vf,
    "-vframes", job.frames
  ]

  if ffmpeg_params:
    ffmpeg.extend(ffmpeg_params.split(" "))

  ffmpeg.extend(["-f", "yuv4mpegpipe", "-"])

  aom = [encoder, "-", "--ivf", f"--fpf={video}.log", f"--threads={args.threads}", "--passes=2"]

  passes = [aom + cmd for cmd in [
    re.sub(r"--denoise-noise-level=[0-9]+", "", encoder_params).split(" ") + ["--pass=1", "-o", os.devnull],
    encoder_params.split(" ") + ["--pass=2", "-o", output_filename]
  ]]

  total_frames = int(job.frames)

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

    if ffmpeg_pipe.poll() is None:
      ffmpeg_pipe.kill()

    if worker.pipe.returncode != 0:
      success = False

  if os.path.isfile(f"{video}.log"):
    os.remove(f"{video}.log")

  if success:
    return output_filename
  else:
    return False

def cancel_job(job):
  client.session.post(
    f"{client.args.target}/cancel_job",
    data={
      "client": job.id,
      "scene": job.scene,
      "projectid": job.projectid
    }
  )

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
    job.ffmpeg_params = r.headers["ffmpeg_params"]
    job.projectid = r.headers["projectid"]
    job.frames = r.headers["frames"]
    job.start = r.headers["start"]
    job.request = r

    if r.headers["version"] != encoder_versions[job.encoder]:
      cancel_job(job)

      if job.encoder == "aom":
        if os.path.isfile("aomenc.exe"):
          os.remove("aomenc.exe")
          client.stop(f"bad aom version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
        else:
          client.stop(f"bad aom version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}")

      if job.encoder == "vpx":
        if os.path.isfile("vpxenc.exe"):
          os.remove("vpxenc.exe")
          client.stop(f"bad vpx version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
        else:
          client.stop(f"bad vpx version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}")

    client.jobs.append(job)

    return job
  except:
    return None

def upload(client, job, file, output):
  files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
  try:
    r = client.session.post(
      f"{client.args.target}/finish_job",
      data={
        "client": job.id,
        "scene": job.scene,
        "projectid": job.projectid,
        "encoder": job.encoder,
        "version": encoder_versions[job.encoder],
        "encoder_params": job.encoder_params,
        "ffmpeg_params": job.ffmpeg_params
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
    self.menu.items = ["add", "remove", "kill", "quit"]
    self.menu.scroll = 0

    self.refreshing = False
    self.screen_thread = Thread(target=self.screen, daemon=True)
    self.refresh = Event()
    self.screen_thread.start()

    self.worker_timer = Event()

    self.exit_event = Event()
    self.exit_message = None

  def stop(self, message=""):
    self.exit_event.set()
    self.exit_message = message

  def add_worker(self, worker):
    self.workers.append(worker)
    worker.start()

  def remove_worker(self, worker):
    if worker in self.workers:
      self.workers.remove(worker)

  def _refresh_screen(self):
    self.header.set_text(f"target: {self.args.target} workers: {self.numworkers} hit: {self.completed} miss: {self.failed}")

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
  
  def key_loop(self, scr):
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
            self.add_worker(new_worker)

        elif menu_action == "remove":
          self.numworkers = max(self.numworkers - 1, 0)
          if any(worker for worker in self.workers if worker.lock_acquired):
            self.worker_timer.set()

        elif menu_action == "kill":
          if len(self.workers) == self.numworkers or any(worker for worker in self.workers if worker.job is None):
            self.numworkers = max(self.numworkers - 1, 0)

          if not any(worker for worker in self.workers if worker.pipe is None):
            sorted_workers = sorted([worker for worker in self.workers if not worker.stopped], key= lambda x: x.progress)
            if len(sorted_workers) > 0:
              sorted_workers[0].status = "killing"
              sorted_workers[0].kill()

          if any(worker for worker in self.workers if worker.lock_acquired):
            self.worker_timer.set()
          
        elif menu_action == "quit":
          for worker in self.workers:
            worker.kill()
          self.stop()

      self.refresh_screen()
  
  def window(self, scr):
    self.scr = scr

    curses.curs_set(0)
    scr.nodelay(0)

    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)

    self.refresh_screen()
    
    k_t = Thread(target=self.key_loop, args=(scr,), daemon=True)
    k_t.start()

    self.exit_event.wait()

    curses.curs_set(1)

class Worker:
  def __init__(self, client):
    self.status = ""
    self.client = client
    self.lock_acquired = False
    self.thread = None
    self.job = None
    self.pipe = None
    self.stopped = False
    self.progress = (0, 0)
    self.id = 0

  def kill(self):
    self.stopped = True
    if self.pipe:
      self.pipe.kill()
    
    if self.job:
      cancel_job(self.job)
    self.client.remove_worker(self)

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

      if not self.lock_acquired:
        self.client.lock.acquire()
        self.lock_acquired = True

      if len(self.client.workers) > self.client.numworkers or self.stopped:
        self.client.lock.release()
        self.client.remove_worker(self)
        return

      self.update_status("downloading")

      while True:
        self.job = fetch_new_job(self.client)
        if self.job: break
        for i in range(0, 15):
          self.client.worker_timer.clear()
          if len(self.client.workers) > self.client.numworkers or self.stopped:
            self.client.lock.release()
            self.client.remove_worker(self)
            return
          self.update_status(f"waiting...{15-i:2d}")
          self.client.worker_timer.wait(1)
          self.client.worker_timer.clear()

      self.client.lock.release()
      self.lock_acquired = False

      with tmp_file("wb", self.job.request, self.job.filename, self.update_status) as file:
        if self.stopped: continue

        if self.job.encoder == "vpx":
          output = aom_vpx_encode(self, "vpxenc", file, self.job, self.update_status)
        elif self.job.encoder == "aom":
          output = aom_vpx_encode(self, "aomenc", file, self.job, self.update_status)
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
                if self.stopped: break
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

def get_aomenc_version():
  if not shutil.which("aomenc"):
    print("aomenc not found, exiting in 3s")
    time.sleep(3)
    exit()
  p = subprocess.run(["aomenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"av1\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

def get_vpxenc_version():
  if not shutil.which("vpxenc"):
    print("vpxenc not found, exiting in 3s")
    time.sleep(3)
    exit()
  p = subprocess.run(["vpxenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"vp9\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

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

  encoder_versions = {"aom": get_aomenc_version(), "vpx": get_vpxenc_version()}

  client = Client(args)

  for i in range(0, int(args.workers)):
    client.add_worker(Worker(client))

  if args.noui:
    for worker in client.workers:
      worker.thread.join()
  else:
    import curses, textwrap
    curses.wrapper(lambda scr: client.window(scr))
    if client.exit_message:
      print(client.exit_message)
      time.sleep(3)
