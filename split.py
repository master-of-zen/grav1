import os, shutil
from util import get_frames, ffmpeg, ffmpeg_pipe
from mkv_keyframes import get_mkv_keyframes
from aom_keyframes import get_aom_keyframes

# returns splits, total frames, segments
# splits are contained like so:
# {
#   "00000": {                # aom segment
#     "segment": "00000.mkv", # split segment
#     "start": 0,             # starting frame within the split segment
#     "frames": 5             # number of frames for the aom segment
#   }
# }
# segments are contained like so:
# {
#   "00000.mkv": {
#     "start": 0,
#     "length": 10
#   }
# }
def split(video, path_split, min_frames=-1, max_frames=-1, cb=None):
  if cb: cb("getting mkv keyframes")
  mkv_keyframes, total_frames = get_mkv_keyframes(video)
  if cb:
    cb(f"total frames: {total_frames}")
    cb(f"src keyframes: {len(mkv_keyframes)}")
  
  skip_keyframes = 0
  aom_keyframes = get_aom_keyframes(video, lambda x: cb(f"getting aom keyframes: {x}/{total_frames}", cr=True))
  if cb:
    cb(f"aom keyframes: {len(aom_keyframes)}")

  if min_frames != -1:
    aom_keyframes.append(total_frames)
    final_scenes = []
    aom_scenes = [(aom_keyframes[i], aom_keyframes[i + 1] - aom_keyframes[i]) for i in range(len(aom_keyframes) - 1)]
    accumulate = 0
    for i, scene in enumerate(aom_scenes[skip_keyframes:]):
      scene = (scene[0] - accumulate, scene[1] + accumulate)
      if scene[1] > min_frames:
        final_scenes.append(scene)
        accumulate = 0
      elif not len(final_scenes):
        accumulate = scene[1]
      else:
        prev_scene = final_scenes[-1]
        if i < len(aom_scenes[skip_keyframes:]) - 1:
          if prev_scene[1] < min_frames:
            final_scenes[-1] = (prev_scene[0], prev_scene[1] + scene[1])
          else:
            next_scene = aom_scenes[skip_keyframes:][i + 1]
            if next_scene[1] + scene[1] < prev_scene[1] + scene[1]:
              accumulate = scene[1]
            else:
              final_scenes[-1] = (prev_scene[0], prev_scene[1] + scene[1])
        else:
          final_scenes[-1] = (prev_scene[0], prev_scene[1] + scene[1])
    aom_keyframes = [s[0] for s in (aom_scenes[:skip_keyframes] + final_scenes)]

  if total_frames not in aom_keyframes:
    aom_keyframes.append(total_frames)

  if max_frames != -1:
    aom_kf = apply_max_dist(aom_keyframes, min_frames, max_frames, mkv_keyframes)
  else:
    aom_kf = aom_keyframes
  
  frames, splits, segments = partition_with_mkv(aom_kf, mkv_keyframes, total_frames)
  reencode = False
  if len(frames) < len(aom_keyframes) / 2:
    splits = {}
    frames = []
    segments = {}

    if max_frames != -1:
      aom_keyframes = apply_max_dist(aom_keyframes, min_frames, max_frames)

    if cb:
      cb("keyframes unreliable, re-encoding")

    for i in range(len(aom_kf) - 1):
      frame = aom_kf[i]
      next_frame = aom_kf[i + 1]
      segment_n = len(frames)
      length = next_frame - frame
      frames.append(frame)
      splits[f"{len(splits):05d}"] = ({
        "segment": f"{segment_n:05d}.mkv",
        "start": 0,
        "frames": length,
        "filesize": 0
      })
      segments[f"{segment_n:05d}.mkv"] = {
        "start": frame,
        "length": length
      }

    reencode = True

  frames = [str(f) for f in frames]

  cmd = [
    "ffmpeg", "-y",
    "-hide_banner",
    "-i", video,
    "-map", "0:v:0",
    "-avoid_negative_ts", "1",
    "-vsync", "0"
  ]

  if reencode: 
    cmd.extend([
      "-c:v", "libx264",
      "-x264-params", "scenecut=-1",
      "-preset", "veryfast",
      "-threads", "16",
      "-crf", "0",
      "-force_key_frames", "expr:" + "+".join([f"eq(n,{int(f)})" for f in frames])
    ])
  else:
    cmd.extend([
      "-c:v", "copy"
    ])

  cmd.extend([
    "-f", "segment",
    "-segment_frames", ",".join(frames[1:]),
    os.path.join(path_split, "%05d.mkv")
  ])

  os.makedirs(path_split, exist_ok=True)
  ffmpeg(cmd, lambda x: cb(f"splitting {x}/{total_frames}", cr=True))

  return splits, total_frames, segments

def apply_max_dist(aom_keyframes, min_dist, max_dist, mkv_keyframes=[], tolerance=5):
  final_kf = [aom_keyframes[0]]
  for i in range(len(aom_keyframes) - 1):
    frame = aom_keyframes[i]
    next_frame = aom_keyframes[i + 1]
    length = next_frame - frame

    while length > max_dist:
      if length - max_dist >= max_dist:
        candidate_kfs = [(f2, abs(frame + max_dist - f2)) for f2 in mkv_keyframes if abs(frame + max_dist - f2) < tolerance]
        if len(candidate_kfs) > 0:
          frame = sorted(candidate_kfs, key=lambda x: x[1])[0][0]
        else:
          frame += max_dist

        length = next_frame - frame
        final_kf.append(frame)
      elif int(length / 2) > min_dist:
        candidate_kfs = [(f2, abs(frame + int(length / 2) - f2)) for f2 in mkv_keyframes if abs(frame + int(length / 2) - f2) < tolerance]
        if len(candidate_kfs) > 0:
          frame = sorted(candidate_kfs, key=lambda x: x[1])[0][0]
        else:
          frame += int(length / 2)
        
        length = next_frame - frame
        final_kf.append(frame)
      else: break

    final_kf.append(next_frame)

  return final_kf

def partition_with_mkv(aom_keyframes, mkv_keyframes, total_frames):
  aom_keyframes = aom_keyframes
  mkv_keyframes = mkv_keyframes + [total_frames]

  splits = {}
  last_end = 0
  frames = []
  segments = {}

  for i in range(len(aom_keyframes) - 1):
    frame = aom_keyframes[i]
    next_frame = aom_keyframes[i+1]
    segment_n = len(frames)
    start = 0
    length = next_frame - frame
    if frame in mkv_keyframes:
      frames.append(frame)
    else:
      largest = 0
      for j in mkv_keyframes:
        if j < frame:
          largest = j
        else:
          break
      start = frame - largest
      if largest in frames or largest < last_end:
        segment_n -= 1
        start = frame - frames[len(frames)-1]
      else:
        frames.append(largest)
    
    splits[f"{len(splits):05d}"] = ({"segment": f"{segment_n:05d}.mkv", "start": start, "frames": length, "filesize": 0})
    last_end = frame + length
  
  for segment_n in range(len(frames)):
    segments[f"{segment_n:05d}.mkv"] = {
      "start": frames[segment_n],
      "length": (total_frames if segment_n == len(frames) - 1 else frames[segment_n + 1]) - frames[segment_n]
    }

  return frames, splits, segments

def write_vs_script(src):
  src = src.replace("\\","\\\\")
  script = f"""from vapoursynth import core
import mvsfunc as mvf
src = core.ffms2.Source("{src}")
mvf.Depth(src, 8).set_output()"""

  open("vs.vpy", "w+").write(script)

def correct_split(path_in, path_out, start, length, cb=None):
  if shutil.which("vspipe"):
    write_vs_script(path_in)
    vspipe_cmd = [
      "vspipe", "vs.vpy",
      "-s", str(start),
      "-e", str(start + length - 1),
      "-y", "-"
    ]
    ffmpeg_cmd = [
      "ffmpeg", "-hide_banner",
      "-i", "-",
      "-c:v", "libx264",
      "-crf", "0",
      "-y", path_out
    ]
    ffmpeg_pipe(vspipe_cmd, ffmpeg_cmd, lambda x: cb(f"correcting split {x}/{length}", cr=True))
  else:
    cmd = [
      "ffmpeg", "-hide_banner",
      "-i", path_in,
      "-map", "0:v:0",
      "-c:v", "libx264",
      "-crf", "0",
      "-vsync", "0",
      "-force_key_frames", f"expr:eq(n,{start})",
      "-x264-params", "scenecut=0",
      "-vf", f"select=gte(n\\,{start})",
      "-frames:v", str(length),
      "-y", path_out
    ]
    ffmpeg(cmd, lambda x: cb(f"correcting split {x}/{length}", cr=True))

# input the source and segments produced by split()
def verify_split(path_in, path_split, segments, cb=None):
  total_frames = 0
  for i, segment in enumerate(segments, start=1):
    path_segment = os.path.join(path_split, segment)
    segment_n = str(os.path.splitext(segment)[0])
    num_frames = get_frames(path_segment)

    if cb: cb(f"verifying splits: {i}/{len(segments)}", cr=True)

    if total_frames != segments[segment]["start"]:
      cb(f"misalignment at {segment} expected: {segments[segment]['start']}, got: {total_frames}")
    elif num_frames != segments[segment]["length"]:
      cb(f"bad framecount {segment} expected: {segments[segment]['length']}, got: {num_frames}")
    else:
      num_frames_slow = get_frames(path_segment, False)
      if num_frames != num_frames_slow:
        cb(f"bad framecount {segment} expected: {num_frames}, got: {num_frames_slow}")
      else:
        total_frames += num_frames
        continue

    os.makedirs(os.path.join(path_split, "old"), exist_ok=True)
    os.rename(path_segment, os.path.join(path_split, "old", segment))
    correct_split(path_in, path_segment, segments[segment]["start"], segments[segment]["length"], lambda x, cr=False: cb(x, cr=cr))
    
    total_frames += num_frames

# this is an example program
if __name__ == "__main__" and False:
  import argparse, json

  parser = argparse.ArgumentParser()
  parser.add_argument("-i", dest="input", required=True)
  parser.add_argument("-o", dest="split_path", required=True)
  parser.add_argument("-s", "--splits", dest="splits", required=True)
  parser.add_argument("--min_frames", default=-1)
  parser.add_argument("--max_frames", default=-1)
  
  args = parser.parse_args()

  splits, total_frames, segments = split(
    args.input,
    args.split_path,
    min_frames=args.min_frames,
    max_frames=args.max_frames,
    cb=lambda x, cr=False: print(x, end="\r" if cr else "\n")
  )

  print(total_frames, "frames")
  print("verifying split")

  verify_split(
    args.input,
    args.split_path,
    segments,
    cb=lambda x, cr=False: print(x, end="\r" if cr else "\n")
  )

  json.dump(splits, open(args.splits, "w+"))
