import sys
import gi

from core.generator import IntrusionAlarmGenerator

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst

from time import monotonic
from typing import List, Dict
from core.utils import parse_buffer2msg
from dataclasses import dataclass

# from algorithms import point_polygon_test
import cv2, numpy as np
from dto import PgieObj

## interface로 빼두어야 함.
POLYGON = [[0, 0], [0, 1080], [960, 1080], [960, 0]]

# AlarmGenerator를 담고 있기엔 이름의 범위가 좁음
# MsgManager가 들고있는 최종 Result는 self.obj_list 여야함.
# 이를 바깥 쪽에서 쓸 수 있으면 좋을 것 같음.
class MsgManager:
    def __init__(self):
        # self.obj_info_list = obj_info_list
        self.strm_list: List = list()
        self.obj_list: List = list()
        self.timeout: float = 3.0
        # 정보 Extract
        # 리스트 업데이트

    # TODO async로 pgie object에 대한 msg를 msg broker를 통해
    # Event processing 모듈로 전달하고 거기서 모든 것이 처리되도록 해야함.
    # 그래야지 deepstream과 alarm generator를 분리할 수 있을 것임.
    def tiler_sink_pad_buffer_probe(self, pad, info, u_data):
        # msg manager
        msg: Dict = dict()
        gst_buffer = info.get_buffer()
        parsed_msg, frame = parse_buffer2msg(gst_buffer, msg)
        self.now = monotonic()
        # print(parsed_msg)
        for frame_info in parsed_msg["frame_list"]:
            for obj_info in frame_info["obj_list"]:
                # pgie_obj생성
                # self.obj_list에 업데이트.
                pgie_obj = PgieObj(obj_info)
                self._update_obj_list(pgie_obj)

        intrusion_alarm_gen = IntrusionAlarmGenerator(self.obj_list, frame)
        intrusion_alarm_gen.run()

        return Gst.PadProbeReturn.OK

    def _update_obj_list(self, pgie_obj):
        # pgie_obj: 현재 등록하려는 obj
        # obj: list에 이미 등록된 obj
        self._register_obj(pgie_obj)
        for obj in self.obj_list:
            self._remove_obj(obj)
            if obj.obj_id == pgie_obj.obj_id:
                obj.last_time = pgie_obj.last_time
                obj.pos = pgie_obj.pos
                obj.bbox = pgie_obj.bbox
                obj.traj.append(pgie_obj.pos)

                obj.update_intrusion_flag(POLYGON)
                obj.update_alarm_state()

        del pgie_obj  # 등록을 마치고 메모리에서 삭제한다.

    def _remove_obj(self, obj):
        # 일정시간이 지난 obj는 list에서 지운다.
        if obj.last_time + self.timeout < self.now:
            self.obj_list.remove(obj)

    def _register_obj(self, pgie_obj):
        # list에 아무 obj가 등록되지 않았거나
        # 새로운 id의 obj가 나타났을 때 등록을 한다.
        obj_id_list = [obj.obj_id for obj in self.obj_list]
        if pgie_obj.obj_id not in obj_id_list:
            self.obj_list.append(pgie_obj)
