#!/usr/bin/env python3
# lightweight client-only for encoding

import os, subprocess, re, contextlib, requests, time, json
from tempfile import NamedTemporaryFile
from zipfile import ZipFile
from io import BytesIO

def print_progress(n, total, size=10, suffix=""):
  fill = "█" * int((n / total) * size)
  remaining = " " * (size - len(fill))
  return f"{int(100 * n / total):3d}%|{fill}{remaining}| {n}/{total}"

def get_frames(input):
  cmd = f"ffmpeg -hide_banner -map 0:v:0 -c copy -f null {os.devnull} -i".split(" ")
  cmd.append(input)
  r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  return int(re.search(r"frame= *([^ ]+?) ", r.stderr.decode("utf-8") + r.stdout.decode("utf-8")).group(1))

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

def vp9_encode(input, encoder_params, args, status_cb):
  output_filename = f"{input}.webm"

  vp9 = f"ffmpeg -y -hide_banner".split(" ")
  vp9.extend(["-i",  input, "-c:v", "libvpx-vp9", "-an", "-passlogfile", f"{input}.log"])
  vp9.extend(encoder_params.split(" "))
  passes = [vp9 + cmd for cmd in [
    ["-pass", "1", "-f", "webm", os.devnull],
    ["-pass", "2", output_filename]
  ]]

  total_frames = get_frames(input)

  pipe = None

  try:
    for pass_n, cmd in enumerate(passes, start=1):
      pipe = subprocess.Popen(cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True)

      status_cb(f"vp9 pass: {pass_n} {print_progress(0, total_frames)}")

      while True:
        line = pipe.stdout.readline().strip()

        if len(line) == 0 and pipe.poll() is not None:
          break

        matches = re.findall(r"frame= *([^ ]+?) ", line)
        if matches:
          status_cb(f"vp9 pass: {pass_n} {print_progress(int(matches[-1]), total_frames)}")
      
      if pipe.returncode != 0:
        status_cb("error")
        return False

    if os.path.isfile(f"{input}.log-0.log"):
      os.remove(f"{input}.log-0.log")

    return output_filename
  except Exception as e:
    print("killing worker")
    if pipe:
      pipe.kill()
    raise e

def aom_encode(input, encoder_params, args, status_cb):
  if len(client.args.vmaf_path) > 0:
    encoder_params = f"{encoder_params} --vmaf-model-path={client.args.vmaf_path}"

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

  pipe = None

  try:
    for pass_n, cmd in enumerate(passes, start=1):
      ffmpeg_pipe = subprocess.Popen(ffmpeg,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)

      pipe = subprocess.Popen(cmd,
        stdin=ffmpeg_pipe.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True)

      status_cb(f"aom pass: {pass_n} {print_progress(0, total_frames)}")

      while True:
        line = pipe.stdout.readline().strip()

        if len(line) == 0 and pipe.poll() is not None:
          break

        match = re.search(r"frame *([^ ]+?)/", line)
        if match:
          status_cb(f"aom pass: {pass_n} {print_progress(int(match.group(1)), total_frames)}")
      
      if pipe.returncode != 0:
        status_cb("error")
        return False

    if os.path.isfile(f"{input}.log"):
      os.remove(f"{input}.log")

    return output_filename
  except Exception as e:
    print("killing worker")
    if pipe:
      pipe.kill()
    raise e

class Client:
  def __init__(self, args):
    self.args = args
    self.workers = []
    self.completed = 0
    self.failed = 0
    self.jobs = []
    self.locked = False

def work(client, status_cb):
  while True:
    status_cb("waiting")

    while client.locked:
      pass

    client.locked = True

    status_cb("downloading")

    jobs_str = json.dumps([{"projectid": job.projectid, "scene": job.scene} for job in client.jobs])

    try:
      r = requests.get(f"{client.args.target}/api/get_job/{jobs_str}", timeout=3)
    except:
      for i in range(0, 15):
        status_cb(f"waiting...{15-i:2d}")
        time.sleep(1)
      client.locked = False
      continue

    if r.status_code != 200 or "success" not in r.headers or r.headers["success"] == "0":
      for i in range(0, 15):
        status_cb(f"waiting...{15-i:2d}")
        time.sleep(1)
      client.locked = False
      continue

    status_cb(f"downloaded {len(r.content)}")

    job = type("", (), {})
    job.id = r.headers["id"]
    job.filename = r.headers["filename"]
    job.scene = r.headers["scene"]
    job.encoder = r.headers["encoder"]
    job.encoder_params = r.headers["encoder_params"]
    job.projectid = r.headers["projectid"]
    job.content = r.content
    client.jobs.append(job)

    client.locked = False
    
    with tmp_file("wb", job.content, job.filename) as file:
      if job.encoder == "vp9":
        output = vp9_encode(file, job.encoder_params, client.args, status_cb)
      elif job.encoder == "aom":
        output = aom_encode(file, job.encoder_params, client.args, status_cb)

      if output:
        status_cb(f"uploading {job.projectid} {job.scene}")
        with open(output, "rb") as file:
          files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
          while True:
            try:
              r = requests.post(client.args.target + "/finish_job", data={"id": job.id, "scene": job.scene, "projectid": job.projectid, "encoder": job.encoder, "encoder_params": job.encoder_params}, files=files)
              break
            except:
              status_cb("unable to connect - trying again")
              time.sleep(1)

          if r.text == "saved":
            client.completed += 1
          else:
            client.failed += 1
            status_cb(f"error {r.status_code}")
            time.sleep(1)
          client.jobs.remove(job)

        while os.path.isfile(output):
          try:
            os.remove(output)
          except:
            time.sleep(1)

    time.sleep(1)

def do(i, client):
  update_status(i, "starting")
  time.sleep(0.1*i)
  work(client, lambda msg: update_status(i, msg))

worker_log = {}
def update_status(i, msg):
  worker_log[i] = msg

def window(scr):
  from curses import curs_set
  scr.nodelay(1)
  curs_set(0)
  while True:
    alive = False if len(client.workers) > 0 else True
    for worker in client.workers:
      if worker.is_alive():
        alive = True
        break

    msg = []
    for worker in worker_log:
      msg.append(worker_log[worker])

    scr.erase()
    scr.addstr(f"target: {args.target} workers: {args.workers} hit: {client.completed} miss: {client.failed}\n")
    scr.addstr("\n".join(msg))
    scr.refresh()

    c = scr.getch()
    if not alive or c == 3:
      break
  curs_set(1)

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
      print("aomenc is missing, downloading...")
      r = requests.get("https://f.grass.moe/f/Sz/aom.zip")
      zipdata = BytesIO()
      zipdata.write(r.content)
      zipfile = ZipFile(zipdata)
      for zipinfo in zipfile.filelist:
        with zipfile.open(zipinfo.filename) as f:
          with open(zipinfo.filename, "wb+") as new_file:
            new_file.write(f.read())
        
    if not os.path.isfile("ffmpeg.exe"):
      print("ffmpeg is missing, downloading...")
      r = requests.get("https://f.grass.moe/f/Sy/ffmpeg.zip")
      zipdata = BytesIO()
      zipdata.write(r.content)
      zipfile = ZipFile(zipdata)
      with zipfile.open("ffmpeg.exe") as f_ffmpeg:
        with open("ffmpeg.exe", "wb+") as f:
          f.write(f_ffmpeg.read())

  from threading import Thread

  client = Client(args)

  for i in range(0, int(args.workers)):
    worker = Thread(target=do, args=(i, client), daemon=True)
    worker.start()
    client.workers.append(worker)

  if args.noui:
    for worker in client.workers:
      worker.join()
  else:
    from curses import wrapper
    wrapper(window)
