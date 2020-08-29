#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, json, shutil
from tempfile import NamedTemporaryFile
from threading import Lock, RLock, Thread, Event, Condition
from concurrent.futures import ThreadPoolExecutor
from collections import deque

bytes_map = ["B", "K", "M", "G"]

KEY_R = ord("R")
KEY_1 = ord("1")
KEY_2 = ord("2")
KEY_3 = ord("3")

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

def aom_vpx_encode(encoder, encoder_path, worker, job):
  worker.job_started = time.time()

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

  output_filename = f"{job.video}.ivf"

  ffmpeg = [
    worker.client.args.ffmpeg, "-y", "-hide_banner",
    "-loglevel", "error",
    "-i", job.video,
    "-strict", "-1",
    "-pix_fmt", "yuv420p",
    "-vf", vf,
    "-vframes", job.frames
  ]

  if ffmpeg_params:
    ffmpeg.extend(ffmpeg_params.split(" "))

  ffmpeg.extend(["-f", "yuv4mpegpipe", "-"])

  aom = [encoder_path, "-", "--ivf", f"--fpf={job.video}.log", f"--threads={args.threads}", "--passes=2"]

  passes = [
    aom + re.sub(r"--denoise-noise-level=[0-9]+", "", encoder_params).split(" ") + ["--pass=1", "-o", os.devnull],
    aom + encoder_params.split(" ") + ["--pass=2", "-o", output_filename]
  ]

  if job.grain:
    if not job.has_grain:
      return False, None
    else:
      passes[1].append(f"--film-grain-table={job.grain}")

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
    worker.update_status(f"{encoder:.3s}", "pass:", pass_n, print_progress(0, total_frames), progress=True)

    while True:
      line = worker.pipe.stdout.readline().strip()

      if len(line) == 0 and worker.pipe.poll() is not None:
        break

      match = re.search(r"frame.*?\/([^ ]+?) ", line)
      if match:
        frames = int(match.group(1))
        worker.progress = (pass_n, frames)
        if pass_n == 2:
          worker.update_fps(frames)
        worker.update_status(f"{encoder:.3s}", "pass:", pass_n, print_progress(frames, total_frames), progress=True)

    if ffmpeg_pipe.poll() is None:
      ffmpeg_pipe.kill()

    if worker.pipe.returncode != 0:
      success = False

  if os.path.isfile(f"{job.video}.log"):
    os.remove(f"{job.video}.log")

  return success, output_filename

class Job:
  def __init__(self, r, video, grain=""):
    self.id = r.headers["id"]
    self.filename = r.headers["filename"]
    self.projectid = r.headers["projectid"]
    self.scene = r.headers["scene"]
    self.encoder = r.headers["encoder"]
    self.encoder_params = r.headers["encoder_params"]
    self.ffmpeg_params = r.headers["ffmpeg_params"]
    self.frames = r.headers["frames"]
    self.start = r.headers["start"]
    self.request = r
    self.has_grain = int(r.headers["grain"]) if "grain" in r.headers else None
    self.video = video
    self.grain = grain

  def dispose(self):
    if self.video and os.path.exists(self.video):
      try:
        os.remove(self.video)
      except: pass
    
    if self.grain and os.path.exists(self.grain):
      try:
        os.remove(self.grain)
      except: pass

class Client:
  def __init__(self, config, encoder_versions, args):
    self.config = config
    self.encoder_versions = encoder_versions
    self.args = args
    self.workers = []
    self.workers_lock = Lock()
    self.numworkers = int(args.workers)
    self.completed = 0
    self.failed = 0
    self.session = requests.Session()
    self.scr = None
    self.render_lock = RLock()
    
    self.menu = type("", (), {})
    self.menu.selected_item = 0
    self.menu.items = ["add", "remove", "kill", "quit"]
    self.menu.scroll = 0

    self.refreshing = False
    self.screen_thread = Thread(target=self.screen, daemon=True)
    self.refresh = Event()
    self.screen_thread.start()

    self.stopping = False
    self.exit_event = Event()
    self.exit_message = None

    self.encode = {
      "aom": lambda worker, job: aom_vpx_encode("aom", args.aomenc, worker, job),
      "vpx": lambda worker, job: aom_vpx_encode("vpx", args.vpxenc, worker, job)
    }

    self.upload_queue = deque()
    self.upload_queue_event = Event()
    self.uploading = None
    Thread(target=self._upload_loop, daemon=True).start()

    self.job_queue_size = int(args.queue)
    self.job_queue = deque()
    self.job_queue_lock = Lock()
    self.job_queue_not_empty = Condition(self.job_queue_lock)
    self.job_queue_ret_lock = Lock()

    self.download_status = ""
    self.download_timer = Event()
    self.download_lock = Lock()
    self.download_event = Event()
    
    self.download_executor = ThreadPoolExecutor(max_workers=1)

    Thread(target=self._download_loop, daemon=True).start()

  def _update_download_status(self, *argv, progress=False):
    self.download_status = " ".join([str(arg) for arg in argv])
    self.refresh_screen()

  def _download_loop(self):
    while True:
      if len(self.job_queue) < self.job_queue_size:
        with self.job_queue_ret_lock:
          job = self.download_job(self._update_download_status)
          if job:
            self._add_job_to_queue(job)
            self.refresh_screen()
      else:
        self.download_status = ""
        self.download_event.wait()
        self.download_event.clear()
      if self.stopping: return

  def _add_job_to_queue(self, job):
    with self.job_queue_not_empty:
      self.job_queue.append(job)
      self.job_queue_not_empty.notify()

  def download_job(self, update_status, worker=None):
    job = self.fetch_new_job(update_status, worker)
    if job:
      if worker:
        worker.job = job
      return job
    for i in range(15):
      if self.stopping or worker and worker.stopped: return None
      update_status(f"waiting...{15-i:2d}")
      self.download_timer.wait(1)
      self.download_timer.clear()
    return None

  def download(self, stream, suffix, cb, worker=None):
    file = ""
    try:
      file = NamedTemporaryFile(mode="wb", suffix=suffix, dir=".", delete=False)
      downloaded = 0
      total_size = int(stream.headers["content-length"])
      for chunk in stream.iter_content(chunk_size=2**16):
        if self.stopping or (worker and worker.stopped):
          if file and file.name:
            os.remove(file.name)
          return None
        if chunk:
          downloaded += len(chunk)
          cb("downloading", print_progress_bytes(downloaded, total_size), progress=True)
          file.write(chunk)
      file.flush()
      file.close()
      return file.name
    except:
      if file and file.name:
        os.remove(file.name)
      return None

  def get_job(self, worker, update_status):
    if self.job_queue_size > 0:
      with self.job_queue_ret_lock:
        return self._get_job_from_queue(worker), True
    else:
      return self._get_job(worker, update_status), False
    
  def _get_job(self, worker, update_status):
    worker.future = self.download_executor.submit(self.download_job, update_status, worker)
    try:
      return worker.future.result()
    except:
      return None

  def _get_job_from_queue(self, worker):
    with self.job_queue_not_empty:
      while len(self.job_queue) == 0:
        self.job_queue_not_empty.wait()
        if worker.stopped: return None
      
      job = self.job_queue.popleft()
      self.download_event.set()
      return job

  def _upload_loop(self):
    while True:
      if len(self.upload_queue) == 0:
        self.upload_queue_event.wait()

      self.upload_queue_event.clear()
      
      try:
        job, output = self.upload_queue.popleft()
        self.uploading = job

        uploads = 3
        fails = 0
        while uploads > 0 and fails < 10:
          r = self._upload(job, output)
          
          if r:
            if r.text == "saved":
              self.completed += 1
              break
            elif r.text == "bad upload":
              if self.args.noui:
                print("bad upload", "retrying", job.projectid, job.scene)
              uploads -= 1
            else:
              if self.args.noui:
                print("failed", r.status_code, r.text, job.projectid, job.scene)
              fails += 1
          else:
            if self.args.noui:
              print("unable to connect, trying again")
            fails += 1
          time.sleep(1)

        if fails >= 10:
          self.failed += 1
        
        if os.path.exists(output):
          try:
            os.remove(output)
          except: pass
      except: pass

      self.uploading = None
      self.refresh_screen()

  def upload(self, job, output):
    self.upload_queue.append((job, output))
    self.upload_queue_event.set()
    self.refresh_screen()

  def _upload(self, job, output):
    try:
      with open(output, "rb") as file:
        files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
        if self.args.noui:
          print("uploading to", f"{self.args.target}/finish_job")
        return self.session.post(
          f"{self.args.target}/finish_job",
          data={
            "client": job.id,
            "scene": job.scene,
            "projectid": job.projectid,
            "encoder": job.encoder,
            "version": encoder_versions[job.encoder],
            "encoder_params": job.encoder_params,
            "ffmpeg_params": job.ffmpeg_params,
            "grain": int(len(job.grain) > 0)
          },
          files=files)
    except:
      return None

  def fetch_grain_table(self, projectid, scene):
    for i in range(3):
      try:
        r = self.session.get(f"{self.args.target}/api/get_grain/{projectid}/{scene}", timeout=3, stream=True)
        if r.status_code == 200:
          return r
      except: pass
    return None

  def fetch_new_job(self, cb, worker=None):
    jobs = [worker.job for worker in self.workers if worker.job is not None]
    jobs.extend(self.job_queue)
    jobs.extend([up[0] for up in self.upload_queue])
    if self.uploading:
      jobs.append(self.uploading)

    jobs = [{"projectid": job.projectid, "scene": job.scene} for job in jobs]

    jobs_str = json.dumps(jobs)
    try:
      r = self.session.get(f"{self.args.target}/api/get_job/{jobs_str}", timeout=3, stream=True)
      if r.status_code != 200:
        return None

      encoder = r.headers["encoder"]
      if self.encoder_versions[encoder] != r.headers["version"]:
        self._cancel_job(r.headers["id"], r.headers["projectid"], r.headers["scene"])
        if encoder == "aom":
          if os.path.isfile("aomenc.exe"):
            self.config["r"] = len(self.workers)
            save_config(self.config)
            os.remove("aomenc.exe")
            self.stop(f"bad aom version. have: {encoder_versions[encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
          else:
            self.stop(f"bad aom version. have: {encoder_versions[encoder]} required: {r.headers['version']}")

        if encoder == "vpx":
          if os.path.isfile("vpxenc.exe"):
            self.config["r"] = len(self.workers)
            save_config(self.config)
            os.remove("vpxenc.exe")
            self.stop(f"bad vpx version. have: {encoder_versions[encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
          else:
            self.stop(f"bad vpx version. have: {encoder_versions[encoder]} required: {r.headers['version']}")

        return None

      video_file = self.download(r, r.headers["filename"], cb, worker)
      if not video_file:
        return None

      if "grain" in r.headers and int(r.headers["grain"]):
        grain_r = self.session.get(f"{self.args.target}/api/get_grain/{r.headers['projectid']}/{r.headers['scene']}", timeout=3, stream=True)
        if grain_r and grain_r.status_code == 200:
          grain_file = self.download(grain_r, r.headers["filename"] + ".table", cb, worker)
          if grain_file:
            return Job(r, video_file, grain_file)
        try:
          os.remove(video_file)
        except: pass
        return None

      return Job(r, video_file)
    except:
      return None

  def _cancel_job(self, id, scene, projectid):
    try:
      self.session.post(
        f"{self.args.target}/cancel_job",
        data={
          "client": id,
          "scene": scene,
          "projectid": projectid
        }
      )
    except: pass

  def cancel_job(self, job):
    self._cancel_job(job.id, job.scene, job.projectid)

  def stop(self, message=""):
    self.stopping = True
    self.download_timer.set()

    for worker in self.workers:
      worker.kill()
      with self.job_queue_not_empty:
        self.job_queue_not_empty.notify_all()
    
    for job in self.job_queue:
      self.cancel_job(job)
      job.dispose()

    self.exit_event.set()
    self.exit_message = message

  def add_worker(self, worker):
    if self.stopping: return
    self.workers.append(worker)
    worker.start()

  def remove_worker(self, worker):
    if worker in self.workers:
      self.workers.remove(worker)
      self.refresh_screen()

  def screen(self):
    while self.refresh.wait():
      if not self.scr: continue
      self.render_lock.acquire()
      msg = []
      for i, worker in enumerate(self.workers, start=1):
        msg.append(f"{i:2} {worker.status}")

      n_active = len([worker for worker in self.workers if worker.pipe])
      n_uploading = len(self.upload_queue) + 1 if self.uploading else 0
      footer = " ".join([f"[{item}]" if i == self.menu.selected_item else f" {item} " for i, item in enumerate(self.menu.items)])

      cfps = round(sum(worker.fps for worker in self.workers), 2)
      self.scr.erase()

      (mlines, mcols) = self.scr.getmaxyx()

      header = []
      for line in textwrap.wrap(f"workers: {self.numworkers} active: {n_active} uploading: {n_uploading} "
        f"hit: {self.completed} miss: {self.failed} cfps: {cfps}", width=mcols):
        header.append(line)

      body_y = len(header)
      window_size = mlines - body_y - 1 - (1 if self.job_queue_size > 0 else 0)
      self.menu.scroll = max(min(self.menu.scroll, len(self.workers) - window_size), 0)

      for i, line in enumerate(header):
        self.scr.insstr(i, 0, line.ljust(mcols), curses.color_pair(1))

      if self.job_queue_size > 0:
        self.scr.insstr(body_y, 0, f"queue: {len(self.job_queue)} {self.download_status}")
        body_y += 1

      for i, line in enumerate(msg[self.menu.scroll:window_size + self.menu.scroll], start=body_y):
        self.scr.insstr(i, 0, line)

      pad = " " * (mcols - len(footer) - len(self.args.target) - 1)
      self.scr.insstr(mlines - 1, 0, f"{footer}{pad}{self.args.target}"[:mcols].ljust(mcols) , curses.color_pair(1))
      
      self.scr.refresh()
      self.refresh.clear()
      self.render_lock.release()

  def refresh_screen(self):
    self.refresh.set()
  
  def key_loop(self, scr):
    while True:
      c = scr.getch()

      if c == curses.KEY_UP:
        self.menu.scroll -= 1
      elif c == curses.KEY_DOWN:
        self.menu.scroll += 1
      elif c == curses.KEY_LEFT:
        self.menu.selected_item = max(self.menu.selected_item - 1, 0)
      elif c == curses.KEY_RIGHT:
        self.menu.selected_item = min(self.menu.selected_item + 1, len(self.menu.items) - 1)
      elif c == 10 or c == curses.KEY_ENTER:
        menu_action = self.menu.items[self.menu.selected_item]

        if menu_action == "add":
          self.numworkers += 1
          with self.workers_lock:
            while len(self.workers) < self.numworkers:
              new_worker = Worker(self)
              self.add_worker(new_worker)

        elif menu_action == "remove":
          self.numworkers = max(self.numworkers - 1, 0)
          
          with self.workers_lock:
            jobless_workers = [worker for worker in self.workers if not worker.job if not worker.future or not worker.future.running()]
            if jobless_workers:
              jobless_workers[0].kill()
              with self.job_queue_not_empty:
                self.job_queue_not_empty.notify_all()

        elif menu_action == "kill":
          self.numworkers = max(self.numworkers - 1, 0)
          
          with self.workers_lock:
            sorted_workers = sorted(
              [worker for worker in self.workers if not worker.stopped],
              key=lambda x: (1 if x.pipe else 0, x.progress, 1 if x.job else 0, 1 if x.future and x.future.running() else 0)
            )
            
            if len(sorted_workers) > 0:
              sorted_workers[0].status = "killing"
              sorted_workers[0].kill()
              with self.job_queue_not_empty:
                self.job_queue_not_empty.notify_all()

              self.remove_worker(sorted_workers[0])
          
        elif menu_action == "quit":
          self.stop()
      elif c == KEY_R:
        with self.render_lock:
          self.scr.clear()
          self.scr.refresh()

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
    for worker in self.workers:
      while worker.thread.is_alive():
        worker.kill()
        time.sleep(1)

    curses.curs_set(1)

class Worker:
  def __init__(self, client):
    self.status = ""
    self.client = client
    self.thread = None
    self.job = None
    self.pipe = None
    self.stopped = False
    self.progress = (0, 0)
    self.id = 0

    self.job_started = 0
    self.fps = 0

    self.future = None

  def kill(self):
    self.stopped = True

    if self.future and not self.future.running():
      self.future.cancel()

    if self.pipe and self.pipe.poll() is None:
      self.pipe.kill()
    
    if self.job:
      self.client.cancel_job(self.job)
      self.job.dispose()

  def start(self):
    self.thread = Thread(target=lambda: self.work(), daemon=True)
    self.thread.start()

  def update_status(self, *argv, progress=False):
    message = " ".join([str(arg) for arg in argv])
    if self.stopped: return
    if self.client.args.noui and not progress:
      print(self.id, message)
    else:
      self.status = message
      self.client.refresh_screen()

  def update_fps(self, frames):
    elapsed = time.time() - self.job_started
    self.fps = frames / elapsed

  def check_job(self):
    for _i in range(3):
      try:
        r = self.client.session.get(f"{self.client.args.target}/api/is_job/{self.job.projectid}/{self.job.scene}", timeout=3)
        if r.status_code == 200:
          return True
        break
      except:
        time.sleep(1)

    self.job.dispose()
    self.job = None
    return False

  def work(self):
    while True:
      self.update_status("waiting", progress=True)

      with self.client.workers_lock:
        if len(self.client.workers) > self.client.numworkers or self.stopped:
          self.client.remove_worker(self)
          return

      self.job, from_queue = self.client.get_job(self, self.update_status)

      if self.stopped:
        if self.job:
          self.job.dispose()
        self.client.remove_worker(self)
        return

      if not self.job:
        continue

      if from_queue:
        self.update_status("checking job")
        if not self.check_job():
          continue

      try:
        success, output = self.client.encode[self.job.encoder](self, self.job)
        if self.pipe and self.pipe.poll() is None:
          self.pipe.kill()

        self.pipe = None

        if success:
          self.client.upload(self.job, output)
          self.job.dispose()
          self.job = None
        elif output:
          if os.path.exists(output):
            try:
              os.remove(output)
            except: pass
      except: pass

    self.client.remove_worker(self)

windows_binaries = [
  ("vmaf_v0.6.1.pkl", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl", "binary"),
  ("vmaf_v0.6.1.pkl.model", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl.model", "binary"),
  ("ffmpeg.exe", "https://www.sfu.ca/~ssleong/ffmpeg.zip", "zip"),
  ("vpxenc.exe", "https://www.sfu.ca/~ssleong/vpxenc.exe", "binary")
]

def get_aomenc_version():
  if not shutil.which(args.aomenc):
    print("aomenc not found, exiting in 3s")
    time.sleep(3)
    exit()
  p = subprocess.run([args.aomenc, "--help"], stdout=subprocess.PIPE)
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

def save_config(config):
  json.dump(config, open("config", "w+"))

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("target", type=str, nargs="?", default="http://localhost:7899")
  parser.add_argument("--vmaf-model-path", dest="vmaf_path", default="vmaf_v0.6.1.pkl" if os.name == "nt" else "")
  parser.add_argument("--workers", dest="workers", default=1)
  parser.add_argument("--threads", dest="threads", default=8)
  parser.add_argument("--noui", action="store_const", const=True)
  parser.add_argument("--aomenc", default="aomenc", help="path to aomenc")
  parser.add_argument("--vpxenc", default="vpxenc", help="path to vpxenc")
  parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg")
  parser.add_argument("--queue", default=0)

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

  if os.path.exists("config"):
    try:
      config = json.load(open("config", "r"))
    except:
      config = {}
  else:
    config = {}

  client = Client(config, encoder_versions, args)

  if args.workers == 1 and "r" in config:
    n_workers = config["r"]
    del config["r"]
    save_config(config)
  else:
    n_workers = args.workers

  for i in range(0, int(n_workers)):
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
