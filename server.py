#!/usr/bin/env python3

import os, subprocess, re, requests, time, sys, json
from datetime import datetime

from flask import Flask, request, send_file, make_response, send_from_directory
from flask_cors import cross_origin
from wsgiserver import WSGIServer

from util import get_frames, parse_time, split, tmp_file, print_progress, ffmpeg

from threading import Thread

path_split = "jobs/{}/split"
path_encode = "jobs/{}/encode"
path_out = "jobs/{}/completed.webm"

re_duration = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}).(\d{2})", re.U)
re_position = re.compile(r".*time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", re.U)

class Project:
  def __init__(self, path_in, encoder_params, threshold, min_frames, max_frames, scene_settings, id=0):
    self.projectid = id or int(time.time())
    self.path_in = path_in
    self.path_out = path_out.format(self.projectid)
    self.path_split = path_split.format(self.projectid)
    self.path_encode = path_encode.format(self.projectid)
    self.log = []
    self.status = "starting"
    self.jobs = {}
    self.threshold = threshold
    self.min_frames = min_frames
    self.max_frames = max_frames
    self.encoder_params = encoder_params
    self.scene_settings = scene_settings
    self.scenes = {}
    self.total_jobs = 0
    
    self.total_frames = get_frames(path_in)

    self.frames = 0
    self.encoded_frames = 0
    self.encode_start = None

    self.total_jobs = 0
    
  def start(self):
    if not os.path.isdir(self.path_split) or len(os.listdir(self.path_split)) == 0:
      self.log.append("splitting")
      self.set_status("splitting")
      split(self.path_in, self.path_split, self.threshold, self.min_frames, self.max_frames)

    scene_filenames = os.listdir(self.path_split)

    self.total_jobs = len(scene_filenames)

    if os.path.isdir(self.path_encode):
      self.log.append("getting resume data")
    
    for scene in scene_filenames:
      scene_n = str(os.path.splitext(scene)[0])
      num_frames = get_frames(os.path.join(self.path_split, scene))

      self.scenes[scene_n] = {
        "filesize": 0,
        "frames": num_frames,
        "encoder_params": ""
      }

      file_ivf = os.path.join(self.path_encode, f"{scene_n}.ivf")
      
      if os.path.isfile(file_ivf):
        self.scenes[scene_n]["filesize"] = os.stat(file_ivf).st_size
        self.frames += num_frames
        self.set_status(f"getting resume data: {self.frames}/{self.total_frames}")
        continue

      if scene_n in self.scene_settings:
        self.scenes[scene_n]["encoder_params"] = self.scene_settings[scene_n]
        scene_setting = self.scene_settings[scene_n]
      else:
        scene_setting = f"{self.encoder_params}"

      self.jobs[scene_n] = Job(
        os.path.join(self.path_split, scene),
        self.projectid,
        scene_setting,
        num_frames
      )

    self.log.append("finished loading")
    self.set_status(print_progress(self.frames, self.total_frames, suffix=f"0fps {len(self.jobs)}/{self.total_jobs} scenes remaining"))
    if os.path.isfile(self.path_out):
      self.set_status("complete")
    else:
      self.complete()

  def complete(self):
    if len(self.jobs) == 0 and self.frames == self.total_frames:
      self.log.append("done! joining files")
      self.concat()
      self.set_status("complete")

  def update_progress(self):
    if self.encode_start: fps = self.encoded_frames / max((datetime.now() - self.encode_start).seconds, 1)
    else: fps = 0
    self.set_status(print_progress(self.frames, self.total_frames, suffix=f"{fps:.2f}fps {len(self.jobs)}/{self.total_jobs} scenes remaining"))

  def set_status(self, msg):
    self.status = msg

  def concat(self):
    scenes = [os.path.join(self.path_encode, f"{os.path.splitext(scene)[0]}.ivf").replace("\\", "/") for scene in self.scenes]
    content = "\n".join([f"file '{scene}'" for scene in scenes])
    with tmp_file("w", content) as file:
      cmd = f"ffmpeg -hide_banner -f concat -safe 0 -y -i".split(" ")
      cmd.extend([file, "-c", "copy", self.path_out])
      ffmpeg(cmd, lambda x: self.set_status(f"{x}, {self.total_frames}"))

class Job:
  def __init__(self, path, projectid, encoder_params, frames):
    self.filename = os.path.basename(path)
    self.scene = os.path.splitext(self.filename)[0]
    self.path = path
    self.encoded_name = f"{self.scene}.ivf"
    self.encoder_params = encoder_params
    self.workers = []
    self.projectid = projectid
    self.frames = frames

def save_projects():
  rtn = {}
  for project in projects:
    p = {}
    p["path_in"] = project.path_in
    p["encoder_params"] = project.encoder_params
    p["threshold"] = project.threshold
    p["min_frames"] = project.min_frames
    p["max_frames"] = project.max_frames
    p["scene_settings"] = project.scene_settings
    rtn[project.projectid] = p

  json.dump(rtn, open("projects.json", "w+"))

def load_projects():
  if not os.path.isfile("projects.json"): return
  ps = json.load(open("projects.json", "r"))
  for pid in ps:
    p = ps[pid]
    project = Project(
      p["path_in"],
      p["encoder_params"],
      p["threshold"],
      p["min_frames"],
      p["max_frames"],
      p["scene_settings"],
      pid)
    projects.append(project)
  
  for project in projects:
    project.start()

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
  return send_from_directory("www", "index.html")

@app.route("/<path:path>", methods=["GET"])
def root(path):
  return send_from_directory("www", path)

@app.route("/scene/<projectid>/<scene>", methods=["GET"])
@cross_origin()
def get_scene(projectid, scene):
  for project in projects:
    if str(projectid) == str(project.projectid):
      return send_from_directory(project.path_encode, scene)
  return ""

@app.route("/api/get_projects", methods=["GET"])
def get_projects():
  rtn = []
  for project in projects:
    p = {}
    p["projectid"] = project.projectid
    p["input"] = project.path_in
    p["frames"] = project.frames
    p["total_frames"] = project.total_frames
    p["jobs"] = len(project.jobs)
    p["total_jobs"] = project.total_jobs
    p["status"] = project.status
    p["encoder_params"] = project.encoder_params
    
    p["scenes"] = project.scenes

    rtn.append(p)
  return json.dumps(rtn)

@app.route("/api/get_job/<jobs>", methods=["GET"])
def get_job(jobs):
  jobs = json.loads(jobs)

  all_jobs = []

  for project in projects:
    all_jobs.extend(project.jobs.values())

  all_jobs = [job for job in all_jobs if not any(job.scene == job2["scene"] and str(job.projectid) == str(job2["projectid"]) for job2 in jobs)]
  all_jobs = sorted(all_jobs, key= lambda x: (len(x.workers), x.frames))
  if len(all_jobs) > 0:
    new_job = all_jobs[0]
    
    workerid = f'{request.environ["REMOTE_ADDR"]}:{request.environ["REMOTE_PORT"]}'
    new_job.workers.append(workerid)

    if not project.encode_start:
      project.encode_start = datetime.now()

    print("sent", new_job.projectid, new_job.scene, "to", workerid)

    resp = make_response(send_file(new_job.path))
    resp.headers["success"] = "1"
    resp.headers["projectid"] = new_job.projectid
    resp.headers["filename"] = new_job.filename
    resp.headers["scene"] = new_job.scene
    resp.headers["id"] = workerid
    resp.headers["encoder_params"] = new_job.encoder_params
    return resp
  else:
    resp = make_response("")
    resp.headers["success"] = "0"
    return resp

@app.route("/finish_job", methods=["POST"])
def receive():
  sender = request.form["id"]
  encoder_params = request.form["encoder_params"]
  projectid = request.form["projectid"]
  scene_number = str(request.form["scene"])
  file = request.files["file"]

  project = None
  for p in projects:
    if str(projectid) == str(p.projectid):
      project = p
      break

  if not project or scene_number not in project.jobs:
    return "bad file", 200

  job = project.jobs[scene_number]

  if job.encoder_params != encoder_params:
    print("discard", project.projectid, scene_number, "bad params")
    return "bad params", 200

  encoded = os.path.join(project.path_encode, job.encoded_name)
  
  if os.path.isfile(encoded):
    print("discard", project.projectid, scene_number, "already done")
    return "already done", 200

  os.makedirs(project.path_encode, exist_ok=True)
  
  file.save(encoded)

  scene = project.scenes[scene_number]
  
  if scene["frames"] != get_frames(encoded):
    os.remove(encoded)
    print("discard", project.projectid, scene_number, "frame mismatch")
    return "bad framecount", 200

  scene["filesize"] = os.stat(encoded).st_size

  project.frames += scene["frames"]
  if sender in job.workers:
    project.encoded_frames += scene["frames"]
    
  del project.jobs[scene_number]

  print("recv", project.projectid, scene_number, "from", sender)

  project.update_progress()

  if len(project.jobs) == 0 and project.frames == project.total_frames:
    print("done", project.projectid)
    Thread(target=lambda: project.complete()).start()
    
  return "saved", 200

@app.route("/api/list_directory", methods=["GET"])
def list_directory():
  return json.dumps(os.listdir("inputfiles"))

@app.route("/api/add_project", methods=["POST"])
def add_project():
  path_input = request.form["input"]

  if not os.path.isfile(path_input): return ""

  encoder_params = request.form["encoder_params"]
  threshold = request.form["threshold"]
  min_frames = request.form["min_frames"]
  max_frames = request.form["max_frames"]
  scene_factor = int(request.form["scene_factor"])

  new_project = Project(path_input, encoder_params, threshold, min_frames, max_frames, {})
  projects.append(new_project)

  save_projects()

  Thread(target=lambda: new_project.start()).start()

  return json.dumps({"success": True, "projectid": new_project.projectid})

if __name__ == "__main__":
  projects = []
  Thread(target=load_projects).start()

  print("listening on 7899")
  WSGIServer(app, port=7899).start()
