# limit the number of cpus used by high performance libraries
import os
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"


import sys
sys.path.insert(0, './yolov5')

import json
import argparse
import os
import platform
import shutil
import time
from pathlib import Path
import cv2
import torch
import torch.backends.cudnn as cudnn
from collections import namedtuple

from yolov5.models.experimental import attempt_load
from yolov5.utils.downloads import attempt_download
from yolov5.models.common import DetectMultiBackend
from yolov5.utils.datasets import LoadImages, LoadStreams
from yolov5.utils.general import (LOGGER, check_img_size, non_max_suppression, scale_coords,
                                  check_imshow, xyxy2xywh, increment_path)
from yolov5.utils.torch_utils import select_device, time_sync
from yolov5.utils.plots import Annotator, colors
from deep_sort.utils.parser import get_config
from deep_sort.deep_sort import DeepSort

print(f"Setup complete. Using torch {torch.__version__} ({torch.cuda.get_device_properties(0).name if torch.cuda.is_available() else 'CPU'})")

# sys.argv = ['track.py', '--source', 'test5.mp4', '--lines-src', 'test5-v2.json', '--classes', '0', '2', '--show-vid', "--conf-thres", "0.5"]


FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # yolov5 deepsort root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative


def detect(opt):
    out, source, lines_src, yolo_model, deep_sort_model, show_vid, save_vid, save_txt, imgsz, evaluate, half, project, name, distance,\
    exist_ok= opt.output, opt.source, opt.lines_src, opt.yolo_model, opt.deep_sort_model, opt.show_vid, opt.save_vid, \
        opt.save_txt, opt.imgsz, opt.evaluate, opt.half, opt.project, opt.name, opt.distance, opt.exist_ok
    webcam = source == '0' or source.startswith(
        'rtsp') or source.startswith('http') or source.endswith('.txt')

    # add lines
    lines = []
    with open(lines_src) as json_file:
        lines = json.load(json_file)
        inside_enters = True
        if "inside_enters" in lines:
            inside_enters = lines.pop("inside_enters")

    # initialize deepsort
    cfg = get_config()
    cfg.merge_from_file(opt.config_deepsort)
    deepsort = DeepSort(deep_sort_model,
                        max_dist=distance,
                        max_iou_distance=cfg.DEEPSORT.MAX_IOU_DISTANCE,
                        max_age=cfg.DEEPSORT.MAX_AGE, n_init=cfg.DEEPSORT.N_INIT, nn_budget=cfg.DEEPSORT.NN_BUDGET,
                        use_cuda=True)

    # Initialize
    device = select_device(opt.device)
    half &= device.type != 'cpu'  # half precision only supported on CUDA

    # The MOT16 evaluation runs multiple inference streams in parallel, each one writing to
    # its own .txt file. Hence, in that case, the output folder is not restored
    if not evaluate:
        if os.path.exists(out):
            pass
            shutil.rmtree(out)  # delete output folder
        os.makedirs(out)  # make new output folder

    # Directories
    save_dir = increment_path(Path(project) / name, exist_ok=exist_ok)  # increment run
    save_dir.mkdir(parents=True, exist_ok=True)  # make dir

    # Load model
    device = select_device(device)
    model = DetectMultiBackend(yolo_model, device=device, dnn=opt.dnn)
    stride, names, pt, jit, _ = model.stride, model.names, model.pt, model.jit, model.onnx
    imgsz = check_img_size(imgsz, s=stride)  # check image size

    # Half
    half &= pt and device.type != 'cpu'  # half precision only supported by PyTorch on CUDA
    if pt:
        model.model.half() if half else model.model.float()

    # Set Dataloader
    vid_path, vid_writer = None, None
    # Check if environment supports image displays
    if show_vid:
        show_vid = check_imshow()

    # Dataloader
    if webcam:
        show_vid = check_imshow()
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz, stride=stride, auto=pt and not jit)
        bs = len(dataset)  # batch_size
    else:
        dataset = LoadImages(source, img_size=imgsz, stride=stride, auto=pt and not jit)
        bs = 1  # batch_size
    vid_path, vid_writer = [None] * bs, [None] * bs

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names

    # extract what is in between the last '/' and last '.'
    txt_file_name = source.split('/')[-1].split('.')[0]
    txt_path = str(Path(save_dir)) + '/' + txt_file_name + '.txt'

    if pt and device.type != 'cpu':
        model(torch.zeros(1, 3, *imgsz).to(device).type_as(next(model.model.parameters())))  # warmup
    dt, seen = [0.0, 0.0, 0.0, 0.0], 0
    # ---------------------
    # Diccionario que para cada objeto contiene:
    # - Su posici??n relativa con respecto a la linea (TOP o BOT)
    # - El frame en que se revis?? por ultima vez al objeto
    # - Su clase (auto o persona)
    objects_positions = {}
    LineData =  namedtuple('LineData', ['inside_area', 'frame'])
    # Diccionario con la cantidad de personas (clase 0) y autos (clase 2) que han entrado y salido al area
    data_dict = {0: {'entra': 0, 'sale': 0}, 2: {'entra': 0, 'sale': 0}}
    total_personas_entran = 0
    total_autos_entran = 0
    FRAMES_TO_SKIP = 5
    # ---------------------
    for frame_idx, (path, img, im0s, vid_cap, s) in enumerate(dataset):
        # ---------------------
        # draw lines to pass
        if "h" not in locals():
            if "youtube" in source:
                h = im0s[0].shape[0]
                w = im0s[0].shape[1]
            else:
                h = im0s.shape[0]
                w = im0s.shape[1]
            line_points = {}
            for line_type in lines.keys():
                line_points[line_type] = []
                for line in lines[line_type]:
                    lx1 = int(w * line["x1"])
                    ly1 = int(h * line["y1"])
                    lx2 = int(w * line["x2"])
                    ly2 = int(h * line["y2"])
                    place = line["place"]
                    line_dict = {
                        "x1": lx1,
                        "y1": ly1,
                        "x2": lx2,
                        "y2": ly2,
                        "place": place
                    }
                    line_points[line_type].append(line_dict)
        # ---------------------

        t1 = time_sync()
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        t2 = time_sync()
        dt[0] += t2 - t1

        # Inference
        visualize = increment_path(save_dir / Path(path).stem, mkdir=True) if opt.visualize else False
        pred = model(img, augment=opt.augment, visualize=visualize)
        t3 = time_sync()
        dt[1] += t3 - t2

        # Apply NMS
        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, opt.classes, opt.agnostic_nms, max_det=opt.max_det)
        dt[2] += time_sync() - t3

        # Process detections
        for i, det in enumerate(pred):  # detections per image
            seen += 1
            if webcam:  # batch_size >= 1
                p, im0, _ = path[i], im0s[i].copy(), dataset.count
                s += f'{i}: '
            else:
                p, im0, _ = path, im0s.copy(), getattr(dataset, 'frame', 0)

            p = Path(p)  # to Path
            save_path = str(save_dir / p.name)  # im.jpg, vid.mp4, ...
            s += '%gx%g ' % img.shape[2:]  # print string

            annotator = Annotator(im0, line_width=2, pil=not ascii)

            # draw lines
            for line in line_points["person"]:
                annotator.line(line["x1"], line["y1"], line["x2"], line["y2"], count=total_personas_entran, color=(0, 0, 255))
            for line in line_points["car"]:
                annotator.line(line["x1"], line["y1"], line["x2"], line["y2"], count=total_autos_entran, color=(255, 0, 0))

            if det is not None and len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(
                    img.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += f"{n} {names[int(c)]}{'s' * (n > 1)}, "  # add to string

                xywhs = xyxy2xywh(det[:, 0:4])
                confs = det[:, 4]
                clss = det[:, 5]

                # pass detections to deepsort
                t4 = time_sync()
                outputs = deepsort.update(xywhs.cpu(), confs.cpu(), clss.cpu(), im0)
                t5 = time_sync()
                dt[3] += t5 - t4

                # draw boxes for visualization
                if len(outputs) > 0:
                    for j, (output, conf) in enumerate(zip(outputs, confs)):

                        bboxes = output[0:4]
                        id = output[4]

                        cls = output[5]

                        c = int(cls)  # integer class

                        # Find middle position of object
                        p_x = (output[0] + output[2]) / 2
                        p_y = (output[1] + output[3]) / 2

                        # Check if object crossed the lines
                        inside_area = True
                        # Person case
                        if cls == 0:
                            obj_class = "person"
                            # Draw object center point red
                            annotator.circle(int(p_x), int(p_y), color=(0, 0, 255))
                        elif cls == 2:
                            obj_class = "car"
                            # Draw object center point blue
                            annotator.circle(int(p_x), int(p_y), color=(255, 0, 0))
                        for line in line_points[obj_class]:
                            lx1 = line["x1"]
                            lx2 = line["x2"]
                            ly1 = line["y1"]
                            ly2 = line["y2"]

                            v1 = (lx2 - lx1, ly2 - ly1)  # Vector 1 (line)
                            v2 = (lx2 - p_x, ly2 - p_y)  # Vector 2 (from point)
                            xp = v1[0] * v2[1] - v1[1] * v2[0]  # Cross product

                            top_or_bot = ''
                            if xp > 0:
                                top_or_bot = 'TOP'
                            elif xp < 0:
                                top_or_bot = 'BOT'
                            if top_or_bot != line["place"]: # If it is outside the area, it stops evaluating other lines
                                inside_area = False
                                break

                        if id in objects_positions:  # if id is in dict
                            # Tienen que pasar al menos n frames para que se considere que el objeto cambio de lado
                            if frame_idx - objects_positions[id].frame > FRAMES_TO_SKIP:
                                if objects_positions[id].inside_area is False and inside_area is True:
                                    if inside_enters:
                                        data_dict[cls]['entra'] += 1
                                    else:
                                        data_dict[cls]['sale'] += 1
                                        # entra_linea += 1
                                elif objects_positions[id].inside_area is True and inside_area is False:
                                    if inside_enters:
                                        data_dict[cls]['sale'] += 1
                                    else:
                                        data_dict[cls]['entra'] += 1
                                        # sale_linea += 1
                                objects_positions[id] = LineData(inside_area, frame_idx)
                        else:
                            objects_positions[id] = LineData(inside_area, frame_idx)
                        total_personas_entran = data_dict[0]['entra'] - data_dict[0]['sale']
                        total_autos_entran = data_dict[2]['entra'] - data_dict[2]['sale']

                        label = f'{id} {names[c]} {conf:.2f} {top_or_bot}'
                        annotator.box_label(bboxes, label, color=colors(c, True))

                        if save_txt:
                            # to MOT format
                            bbox_left = output[0]
                            bbox_top = output[1]
                            bbox_w = output[2] - output[0]
                            bbox_h = output[3] - output[1]
                            # Write MOT compliant results to file
                            with open(txt_path, 'a') as f:
                                f.write(('%g ' * 10 + '\n') % (frame_idx + 1, id, bbox_left,  # MOT format
                                                               bbox_top, bbox_w, bbox_h, -1, -1, -1, -1))

                LOGGER.info(f'{s}Done. YOLO:({t3 - t2:.3f}s), DeepSort:({t5 - t4:.3f}s), total_personas_entran: {total_personas_entran}, total_autos_entran: {total_autos_entran}\n')

                # Write output to file
                with open('../frontend_Moris/frontend/output.csv', 'a') as file:
                    file.write(f"{frame_idx},{data_dict[0]['entra']},{data_dict[0]['sale']},{data_dict[2]['entra']},{data_dict[2]['sale']}\n")

            else:
                deepsort.increment_ages()
                LOGGER.info('No detections')

            # Stream results
            im0 = annotator.result()
            if show_vid:
                # RESIZE IMAGE
                scale = 0.5
                width = int(im0.shape[1] * scale)
                height = int(im0.shape[0] * scale)
                cv2.imshow(str(p), cv2.resize(im0, dsize=(width, height)))
                if cv2.waitKey(1) == ord('q'):  # q to quit
                    raise StopIteration

            # Save results (image with detections)
            if save_vid:
                if vid_path != save_path:  # new video
                    vid_path = save_path
                    if isinstance(vid_writer, cv2.VideoWriter):
                        vid_writer.release()  # release previous video writer
                    if vid_cap:  # video
                        fps = vid_cap.get(cv2.CAP_PROP_FPS)
                        w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    else:  # stream
                        fps, w, h = 30, im0.shape[1], im0.shape[0]

                    vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
                vid_writer.write(im0)

    # Print results
    t = tuple(x / seen * 1E3 for x in dt)  # speeds per image
    LOGGER.info(f'Speed: %.1fms pre-process, %.1fms inference, %.1fms NMS, %.1fms deep sort update \
        per image at shape {(1, 3, *imgsz)}' % t)
    if save_txt or save_vid:
        print('Results saved to %s' % save_path)
        if platform == 'darwin':  # MacOS
            os.system('open ' + save_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yolo_model', nargs='+', type=str, default='yolov5m.pt', help='model.pt path(s)')
    parser.add_argument('--deep_sort_model', type=str, default='osnet_x0_25')
    parser.add_argument('--source', type=str, default='0', help='source')  # file/folder, 0 for webcam
    parser.add_argument('--lines-src', type=str, default=None, help='lines JSON source file')
    parser.add_argument('--output', type=str, default='inference/output', help='output folder')  # output folder
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640], help='inference size h,w')
    parser.add_argument('--conf-thres', type=float, default=0.3, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--fourcc', type=str, default='mp4v', help='output video codec (verify ffmpeg support)')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--show-vid', action='store_true', help='display tracking video results')
    parser.add_argument('--save-vid', action='store_true', help='save video tracking results')
    parser.add_argument('--save-txt', action='store_true', help='save MOT compliant results to *.txt')
    # class 0 is person, 1 is bycicle, 2 is car... 79 is oven
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 16 17')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--evaluate', action='store_true', help='augmented inference')
    parser.add_argument("--config_deepsort", type=str, default="deep_sort/configs/deep_sort.yaml")
    parser.add_argument("--half", action="store_true", help="use FP16 half-precision inference")
    parser.add_argument('--visualize', action='store_true', help='visualize features')
    parser.add_argument('--max-det', type=int, default=1000, help='maximum detection per image')
    parser.add_argument('--dnn', action='store_true', help='use OpenCV DNN for ONNX inference')
    parser.add_argument('--project', default=ROOT / 'runs/track', help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')

    parser.add_argument('--distance', type=float, default=0.2, help='max distance to match')
    opt = parser.parse_args()
    opt.imgsz *= 2 if len(opt.imgsz) == 1 else 1  # expand

    # Create output file
    with open('../frontend_Moris/frontend/output.csv', 'w') as file:
        file.write("frame,p_in,p_out,c_in,c_out\n")
    with torch.no_grad():
        detect(opt)
