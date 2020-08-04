#!/usr/bin/env python3

import subprocess

import os, re, json, shutil, logging
from threading import Thread, Event

from logger import NET
from logger import setup as setup_logging

from project import Projects, Project

from flask import Flask, request, send_file, make_response, send_from_directory
from flask_cors import cross_origin
from wsgiserver import WSGIServer

app = Flask(__name__)

@app.route("/scene/<projectid>/<scene>", methods=["GET"])
@cross_origin()
def get_scene(projectid, scene):
  if projectid not in projects:
    return "", 404
  return send_from_directory(projects[projectid].path_encode, scene)

@app.route("/completed/<projectid>", methods=["GET"])
@cross_origin()
def get_completed(projectid):
  if projectid not in projects:
    return "", 404
  return send_file(projects[projectid].path_out)

@app.route("/api/get_project/<projectid>", methods=["GET"])
@cross_origin()
def get_project(projectid):
  if projectid not in projects:
    return "", 404

  project = projects[projectid]

  p = {}
  p["projectid"] = project.projectid
  p["input"] = project.path_in
  p["frames"] = project.get_frames()
  p["total_frames"] = project.input_total_frames
  p["jobs"] = len(project.jobs)
  p["total_jobs"] = project.total_jobs
  p["status"] = project.status
  p["encoder_params"] = project.encoder_params
  p["ffmpeg_params"] = project.ffmpeg_params
  p["encoder"] = project.encoder
  p["scenes"] = project.scenes
  p["priority"] = project.priority
  p["workers"] = [job for job in project.jobs if len(project.jobs[job].workers) > 0]

  return json.dumps(p)

@app.route("/api/get_projects", methods=["GET"])
@cross_origin()
def get_projects():
  rtn = []
  for project in projects.values():
    p = {}
    p["projectid"] = project.projectid
    p["input"] = project.path_in
    p["frames"] = project.get_frames()
    p["total_frames"] = project.input_total_frames
    p["jobs"] = len(project.jobs)
    p["total_jobs"] = project.total_jobs
    p["status"] = project.status
    p["size"] = sum([project.scenes[scene]["filesize"] for scene in project.scenes])
    p["priority"] = project.priority

    rtn.append(p)
  return json.dumps(rtn)

@app.route("/api/get_grain/<projectid>/<scene>", methods=["GET"])
def get_grain(projectid, scene):
  if projectid not in projects:
    return "", 404

  if not projects[projectid].grain:
    return "", 404

  return send_from_directory(projects[projectid].path_grain, f"{scene}.table")

@app.route("/api/get_job/<jobs>", methods=["GET"])
def get_job(jobs):
  jobs = json.loads(jobs)
  
  ip_list = request.headers.getlist("X-Forwarded-For")

  workerid = f"{ip_list[0] if ip_list else request.remote_addr}:{request.environ.get('REMOTE_PORT')}"

  new_job = projects.get_job(jobs, workerid)

  if not new_job:
    return "", 404

  logging.log(NET, "sent", new_job.project.projectid, new_job.scene, "to", workerid, new_job.frames)

  resp = make_response(send_file(new_job.path))
  resp.headers["projectid"] = new_job.project.projectid
  resp.headers["filename"] = new_job.filename
  resp.headers["scene"] = new_job.scene
  resp.headers["id"] = workerid
  resp.headers["encoder"] = new_job.encoder
  resp.headers["encoder_params"] = new_job.encoder_params
  resp.headers["ffmpeg_params"] = new_job.ffmpeg_params
  resp.headers["version"] = versions[new_job.encoder]
  resp.headers["start"] = new_job.start
  resp.headers["frames"] = new_job.frames
  resp.headers["grain"] = int(new_job.grain)
  return resp

@app.route("/cancel_job", methods=["POST"])
def cancel_job():
  client = request.form["client"] if "client" in request.form else request.form["id"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])

  with projects.projects_lock:
    if projectid not in projects:
      return "project not found", 404

    project = projects[projectid]

    if scene_number not in project.jobs:
      return "job not found", 404

    job = project.jobs[scene_number]

    if client in job.workers:
      job.workers.remove(client)
      logging.log(NET, "cancel", projectid, scene_number, "by", client)

  return "saved", 200

@app.route("/finish_job", methods=["POST"])
def receive():
  client = request.form["client"]
  encoder = request.form["encoder"]
  version = request.form["version"]

  if version != versions[encoder]:
    return "bad encoder version", 200

  encoder_params = request.form["encoder_params"]
  ffmpeg_params = request.form["ffmpeg_params"]
  projectid = str(request.form["projectid"])
  scene_number = str(request.form["scene"])
  grain = int(request.form["grain"]) if "grain" in request.form else False
  file = request.files["file"]

  return projects.check_job(projectid, client, encoder, encoder_params, ffmpeg_params, scene_number, grain, file), 200

@app.route("/api/list_directory", methods=["GET"])
@cross_origin()
def list_directory():
  return json.dumps(os.listdir("inputfiles"))

@app.route("/api/delete_project/<projectid>", methods=["POST"])
@cross_origin()
def delete_project(projectid):
  content = request.json
  if password and ("password" not in content or content["password"] != password):
    logging.log(NET, "Bad password.")
    return json.dumps({"success": False, "reason": "Bad password."})

  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  projects[projectid].stopped = True
  del projects[projectid]

  return json.dumps({"success": True})

@app.route("/api/modify/<projectid>", methods=["POST"])
@cross_origin()
def modify_project(projectid):
  changes = request.json
  if password and ("password" not in changes or changes["password"] != password):
    logging.log(NET, "Bad password.")
    return json.dumps({"success": False, "reason": "Bad password."})

  if projectid not in projects:
    return json.dumps({"success": False, "reason": "Project does not exist."})

  project = projects[projectid]

  if "priority" in changes:
    if not isinstance(changes["priority"], (int, float)):
      return json.dumps({
        "success": False,
        "reason": "priority must be a number"
      })
    project.priority = changes["priority"]

  if "on_complete" in changes:
    project.action = changes["on_complete"]

  return json.dumps({"success": True})

@app.route("/api/add_project", methods=["POST"])
@cross_origin()
def add_project():
  content = request.json
  if password and ("password" not in content or content["password"] != password):
    logging.log(NET, "Bad password.")
    return json.dumps({"success": False, "reason": "Bad password."})

  missing_fields = ",".join([key for key in ["input", "encoder", "encoder_params"] if key not in content])
  if missing_fields:
    return json.dumps({"success": False, "reason": f"Missing fields {missing_fields}"})

  if not (isinstance(content["min_frames"], int) and \
    isinstance(content["max_frames"], int)):
    return json.dumps({
      "success": False,
      "reason": "min_frames and max_frames must be of type integer"
    })

  if not isinstance(content["priority"], (int, float)):
    return json.dumps({
      "success": False,
      "reason": "priority must be a number"
    })
  
  if not content["input"]:
    return json.dumps({"success": False, "reason": "input is empty"})

  missing_files = ",".join([f for f in content["input"] if not os.path.isfile(f)])
  if missing_files:
    return json.dumps({"success": False, "reason": f"Input files not found: {missing_files}"})
  
  if "id" in content and content["id"]:
    if len(content["input"] > 1):
      existing_projects = ",".join(f"{content['id']}{i + 1:02d}" for i in range(content["input"]) if f"{content['id']}{i + 1:02d}" in projects)
      if existing_projects:
        return json.dumps({"success": False, "reason": f"Project with ids {existing_projects} already exist"})
    else:
      if content["id"] in projects:
        return json.dumps({"success": False, "reason": f"Project with id {content['id']} already exist"})
  
  for i, input_file in enumerate(content["input"], 1):
    logging.log(NET, "add project", input_file)

    if "id" in content and content["id"]:
      id = f"{content['id']}{i:02d}"
    else:
      id = 0

    projects.add(Project(
      input_file,
      projects.path_jobs, 
      content["encoder"],
      content["encoder_params"],
      ffmpeg_params=content["ffmpeg_params"] if "ffmpeg_params" in content else "",
      min_frames=content["min_frames"] if "min_frames" in content else -1,
      max_frames=content["max_frames"] if "max_frames" in content else -1,
      priority=content["priority"] if "priority" in content else 0,
      id=id
    ), content["on_complete"] if "on_complete" in content else "")

  return json.dumps({"success": True})

@app.route("/api/get_home", methods=["GET"])
@cross_origin()
def get_home():
  info = {
    "versions": {
      "libaom": versions["aom"],
      "libvpx": versions["vpx"],
      "dav1d": versions["dav1d"]
    },
    "projects": len(projects),
    "jobs": len([job for pid in projects.projects for job in projects[pid].jobs]),
    "frames per hour": {
      "since": projects.telemetry["fph_time"],
      "frames": projects.telemetry["fph"]
    }
  }
  return json.dumps(info)

@app.route("/api/get_info", methods=["GET"])
@cross_origin()
def get_info():
  info = {
    "encoders": {
      "aomenc": versions["aom"],
      "vpxenc": versions["vpx"]
    },
    "actions": list(projects.actions.keys()),
    "protocols": ["http-get"],
    "logs": list(logging._levelToName.values()),
    "password": password is not None
  }
  return json.dumps(info)

def get_dav1d_version():
  if not shutil.which("dav1d"):
    print("dav1d not found")
    exit(1)

  p = subprocess.run(["dav1d", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  return p.stdout.decode("utf-8").strip() + p.stderr.decode("utf-8").strip()

def get_aomenc_version():
  if not shutil.which("aomenc"):
    print("aomenc not found")
    exit(1)
    
  p = subprocess.run(["aomenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"av1\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

def get_vpxenc_version():
  if not shutil.which("vpxenc"):
    print("vpxenc not found")
    exit(1)

  p = subprocess.run(["vpxenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"vp9\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--port", default=7899)
  parser.add_argument("--cwd", default=os.getcwd())
  parser.add_argument("--password", default=None)
  args = parser.parse_args()

  password = args.password

  setup_logging()

  if password:
    logging.info("Starting with protected add, modify, and delete")

  from grav1ty.util import vs_core

  if vs_core:
    logging.info("Vapoursynth supported")

  path_split = os.path.join(args.cwd, "jobs/{}/split")
  path_encode = os.path.join(args.cwd, "jobs/{}/encode")
  path_out = os.path.join(args.cwd, "jobs/{}/completed.webm")

  logging.info("Working directory:", args.cwd)

  versions = {
    "aom": get_aomenc_version(),
    "vpx": get_vpxenc_version(),
    "dav1d": get_dav1d_version()
  }

  projects = Projects(args.cwd)

  projects.load_projects()

  logging.info("listening on port", args.port)
  WSGIServer(app, port=int(args.port)).start()
