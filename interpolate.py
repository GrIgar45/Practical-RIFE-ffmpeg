import argparse
import json
import os
import shlex
import subprocess as sp
import threading
import warnings
from queue import Queue

import numpy as np
import torch
from torch.nn import functional as F
from tqdm import tqdm

warnings.filterwarnings("ignore")


# https://stackoverflow.com/questions/66098161/how-do-i-reliably-find-the-frame-count-of-a-video-in-less-than-a-few-seconds
def getFramesCout():
    import re
    command = f"ffmpeg -i {args.input_video_name} -map 0:v:0 -c copy -f null -"
    command = shlex.split(command)
    ffmpegOutput = sp.check_output(command, stderr=sp.STDOUT).decode('utf-8')
    ffmpegOutput = ffmpegOutput.split('\r')
    for line in reversed(ffmpegOutput):
        result = re.search('frame=(.*)fps', line)
        if result is not None:
            return int(result.group(1))
    return None


parser = argparse.ArgumentParser(description='Interpolation for a pair of images')
parser.add_argument('-i', dest='input_video_name', required=True, type=str, default=None)
parser.add_argument('-o', dest='output_video_name', type=str, default=None)
parser.add_argument('--fp16', dest='fp16', action='store_true',
                    help='fp16 mode for faster and more lightweight inference on cards with Tensor Cores')
parser.add_argument('-U', dest='UHD', action='store_true', help='support 4k video')
parser.add_argument('-s', dest='scale', type=float, default=1.0, choices=[0.25, 0.5, 1.0, 2.0, 4.0],
                    help='Try scale=0.5 for 4k video')
parser.add_argument('-x', dest='multi', type=int, default=2)
parser.add_argument('-wbs', dest='wbs', type=int, default=2000, help='write buffer size before encoding')
args = parser.parse_args()

# https://gist.github.com/oldo/dc7ee7f28851922cca09
cmd = shlex.split(f"ffprobe -v quiet -print_format json -show_streams -select_streams v:0 {args.input_video_name}")
# run the ffprobe process, decode stdout into utf-8 & convert to JSON
ffprobeOutput = sp.check_output(cmd).decode('utf-8')
# print(ffprobeOutput)
ffprobeOutput = json.loads(ffprobeOutput)

width = int(ffprobeOutput['streams'][0]['width'])
height = int(ffprobeOutput['streams'][0]['height'])
fps_original = eval(ffprobeOutput['streams'][0]['r_frame_rate'])
fps_target = fps_original * args.multi
total_frames = getFramesCout()

command = shlex.split(f'ffmpeg -i {args.input_video_name} -f rawvideo -pix_fmt rgb24 -')
input_pipe = sp.Popen(command, stdout=sp.PIPE, stderr=sp.DEVNULL, pipesize=1048576)

def read_raw():
    raw = input_pipe.stdout.read(height * width * 3)
    if not raw:
        return None
    return raw

# setup output
write_buffer = Queue(maxsize=args.wbs)
def writting_frames():
    total_encoding_frames = total_frames * args.multi - (args.multi - 1)
    pbarenc = tqdm(total=total_encoding_frames, desc="encoding", position=1, ascii=' ⡀⡄⡆⡇⡏⡟⡿⣿')
    if args.output_video_name is None:
        video_path_wo_ext, ext = os.path.splitext(args.input_video_name)
        ext = ext[1:]
        args.output_video_name = '{}_{}X_{}fps.{}'.format(video_path_wo_ext, args.multi, int(np.round(fps_target)), ext)
    x265_params = "limit-sao=1:bframes=8:psy-rd=1.5:psy-rdoq=2:aq-mode=3"
    command = shlex.split(f'ffmpeg -y -i {args.input_video_name} '
                          f'-f rawvideo -s {width}x{height} -pixel_format rgb24 -r {fps_target} -i pipe: '
                          '-map 0 -map -0:v -map 1:v '
                          f'-c:v libx265 -x265-params "{x265_params}" -preset medium '
                          f'-pix_fmt yuv420p10le -crf 23 {args.output_video_name}')
    output_pipe = sp.Popen(command, stdin=sp.PIPE, stderr=sp.DEVNULL)
    while True:
        item = write_buffer.get()
        if item is None:
            break
        output_pipe.stdin.write(item)
        pbarenc.update()

    output_pipe.stdin.close()
    output_pipe.wait()
threading.Thread(target=writting_frames).start()


def make_inference(I0, I1, n):
    global model
    if model.version >= 3.9:
        res = []
        for i in range(n):
            res.append(model.inference(I0, I1, (i + 1) * 1. / (n + 1), args.scale))
        return res
    else:
        middle = model.inference(I0, I1, args.scale)
        if n == 1:
            return [middle]
        first_half = make_inference(I0, middle, n=n // 2)
        second_half = make_inference(middle, I1, n=n // 2)
        if n % 2:
            return [*first_half, middle, *second_half]
        else:
            return [*first_half, *second_half]


def pad_image(img):
    if (args.fp16):
        return F.pad(img, padding).half()
    else:
        return F.pad(img, padding)


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.set_grad_enabled(False)
if torch.cuda.is_available():
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    if (args.fp16):
        torch.set_default_tensor_type(torch.cuda.HalfTensor)

try:
    from train_log.RIFE_HDv3 import Model
except:
    print("Please download our model from model list")
model = Model()
if not hasattr(model, 'version'):
    model.version = 0
model.load_model("train_log", -1)
# print("Loaded 3.x/4.x HD model.")
model.eval()
model.device()

pbar = tqdm(total=total_frames, desc="interpol", position=0, ascii=' ⡀⡄⡆⡇⡏⡟⡿⣿')
raw_data = read_raw()
write_buffer.put(raw_data)
pbar.update()

tmp = max(128, int(128 / args.scale))
ph = ((height - 1) // tmp + 1) * tmp
pw = ((width - 1) // tmp + 1) * tmp
padding = (0, pw - width, 0, ph - height)
I1 = torch.from_numpy(np.transpose(np.frombuffer(raw_data, dtype=np.uint8).reshape((height, width, 3)), (2, 0, 1))) \
         .to(device, non_blocking=True).unsqueeze(0).float() / 255.
I1 = pad_image(I1)
temp = None  # save last_frame when processing static frame

while True:
    raw_data = read_raw()
    if raw_data is None:
        break
    I0 = I1
    I1 = torch.from_numpy(np.transpose(np.frombuffer(raw_data, dtype=np.uint8).reshape((height, width, 3)), (2, 0, 1))) \
             .to(device, non_blocking=True).unsqueeze(0).float() / 255.
    I1 = pad_image(I1)

    output = make_inference(I0, I1, args.multi - 1)
    for mid in output:
        mid = ((mid[0] * 255.).byte().cpu().numpy().transpose(1, 2, 0))
        write_buffer.put(mid[:height, :width].tobytes())
    write_buffer.put(raw_data)
    pbar.update()

# notify about finish of interpolation process
input_pipe.stdout.close()
write_buffer.put(None)
input_pipe.wait()
