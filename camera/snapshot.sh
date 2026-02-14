#!/bin/bash

DIM="${1-1920x1080}"
TS=$(date +%s)

cd /home/bisenbek/projects/esp/camera

mkdir -p ./snapshots &>/dev/null

ffmpeg \
	-f video4linux2 \
	-s ${DIM} \
	-i /dev/video0 \
	-ss 0:0:2 -frames 1 \
	./snapshots/${TS}.jpg 2>&1 \
		| tee ./snapshots/log.txt &>/dev/null

cp ./snapshots/${TS}.jpg ./snapshot.jpg

