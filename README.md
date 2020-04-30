# grav1
## distributed network av1 / vp9 encoder

[eight megabyte encodes](https://grass.moe/8mb.html)

### deps
server:
```
flask
flask-cors
wsgiserver
```
client:
```
requests
```
```
curses
ffmpeg
aomenc (vmaf optional)
```

### usage

this only supports aomenc and libvpx-vp9 provided by ffmpeg  
(I don't plan on supporting other encoders)

start webserver (default port: 7899)  
`python server.py`  
`python server.py --port 1234`

starting up a worker  
`python grav1c.py http://target --workers=4`  
