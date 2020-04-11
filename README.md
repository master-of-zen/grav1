# grav1
## distributed network av1 encoder

### why?
simple reason: why not  
av1 is so hard already and I don't have a threadripper

### deps
```
python3
ffmpeg
aomenc (vmaf optional)
```

### usage

this only supports aomenc (libaom from google/mozilla)  
(I don't plan on supporting other encoders)

starting a server for encoding a file  
`python grav1.py -i INPUT output.mp4`

starting up a worker  
`python grav1.py http://server:7789`  
`python grav1c.py http://server:7789 --workers=4`  

### is network encoding even feasible?
yes.  
by segmenting the input video, each clip may only be a mew megabytes maximum  
and when it is encoded with av1, the resulting file size is tiny

### how is this different from av1an master-server?
i dunno  
but this is designed specifically for throwing up workers on random computers  
this uses a flask http server to serve jobs and the server doesn't have to worry about anything