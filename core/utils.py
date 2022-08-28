import sys
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst
import pyds
import ctypes
import numpy as np
import cv2

from typing import Dict, List

from core.manageDB import retrieve_pgie_obj, PgieObj

fps_streams = {}
frame_count = {}
saved_count = {}
PGIE_CLASS_ID_PERSON = 0

MAX_DISPLAY_LEN = 64
MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 4000000
TILED_OUTPUT_WIDTH = 1920
TILED_OUTPUT_HEIGHT = 1080
GST_CAPS_FEATURES_NVMM = "memory:NVMM"
pgie_classes_str = ["Person"]

MIN_CONFIDENCE = 0.3
MAX_CONFIDENCE = 0.4


class inference_parameter:
    def __init__(self):
        self.folder_name: str


def layer_finder(output_layer_info, name):
    """Return the layer contained in output_layer_info which corresponds
    to the given name.
    """
    for layer in output_layer_info:
        # dataType == 0 <=> dataType == FLOAT
        if layer.dataType == 0 and layer.layerName == name:
            return layer
    return None


def make_elm_or_print_err(factoryname, name, printedname, detail=""):
    """Creates an element with Gst Element Factory make.
    Return the element  if successfully created, otherwise print
    to stderr and return None.
    """
    print("Creating", printedname)
    elm = Gst.ElementFactory.make(factoryname, name)
    if not elm:
        sys.stderr.write("Unable to create " + printedname + " \n")
        if detail:
            sys.stderr.write(detail)
    return elm


def cb_newpad(decodebin, decoder_src_pad, data):
    print("In cb_newpad\n")
    caps = decoder_src_pad.get_current_caps()
    gststruct = caps.get_structure(0)
    gstname = gststruct.get_name()
    source_bin = data
    features = caps.get_features(0)

    # Need to check if the pad created by the decodebin is for video and not
    # audio.
    print("gstname=", gstname)
    if gstname.find("video") != -1:
        # Link the decodebin pad only if decodebin has picked nvidia
        # decoder plugin nvdec_*. We do this by checking if the pad caps contain
        # NVMM memory features.
        print("features=", features)
        if features.contains("memory:NVMM"):
            # Get the source bin ghost pad
            bin_ghost_pad = source_bin.get_static_pad("src")
            if not bin_ghost_pad.set_target(decoder_src_pad):
                sys.stderr.write(
                    "Failed to link decoder src pad to source bin ghost pad\n"
                )
        else:
            sys.stderr.write(" Error: Decodebin did not pick nvidia decoder plugin.\n")


def decodebin_child_added(child_proxy, Object, name, user_data):
    print("Decodebin child added:", name, "\n")
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)


def create_source_bin(index, uri):
    print("Creating source bin")

    # Create a source GstBin to abstract this bin's content from the rest of the
    # pipeline
    bin_name = "source-bin-%02d" % index
    print(bin_name)
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write(" Unable to create source bin \n")

    # Source element for reading from the uri.
    # We will use decodebin and let it figure out the container format of the
    # stream and the codec and plug the appropriate demux and decode plugins.
    uri_decode_bin = Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
    if not uri_decode_bin:
        sys.stderr.write(" Unable to create uri decode bin \n")
    # We set the input uri to the source element
    uri_decode_bin.set_property("uri", uri)
    # Connect to the "pad-added" signal of the decodebin which generates a
    # callback once a new pad for raw data has beed created by the decodebin
    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)

    # We need to create a ghost pad for the source bin which will act as a proxy
    # for the video decoder src pad. The ghost pad will not have a target right
    # now. Once the decode bin creates the video decoder and generates the
    # cb_newpad callback, we will set the ghost pad target to the video decoder
    # src pad.
    Gst.Bin.add(nbin, uri_decode_bin)
    bin_pad = nbin.add_pad(Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC))
    if not bin_pad:
        sys.stderr.write(" Failed to add ghost pad in source bin \n")
        return None
    return nbin


def parse_buffer2msg(buffer, msg):

    gst_buffer = buffer
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list

    frame_list: List = list()
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_meta_contents = {
            "source_id": frame_meta.source_id,
            "source_height": frame_meta.source_frame_height,
            "source_width": frame_meta.source_frame_width,
            "source_time": frame_meta.ntp_timestamp,
        }

        l_obj = frame_meta.obj_meta_list
        obj_list: List = list()
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break

            # ---- Stacking object meta data ---- #
            obj_meta_contents: Dict = dict()
            obj_meta_contents["obj_id"] = obj_meta.object_id
            obj_meta_contents["obj_confid"] = obj_meta.confidence
            obj_meta_contents["obj_class_id"] = obj_meta.class_id
            obj_meta_contents["obj_class_label"] = obj_meta.obj_label

            bbox_info_contents: Dict = dict()
            bbox_info_contents[
                "height"
            ] = obj_meta.tracker_bbox_info.org_bbox_coords.height
            bbox_info_contents["left"] = obj_meta.tracker_bbox_info.org_bbox_coords.left
            bbox_info_contents["top"] = obj_meta.tracker_bbox_info.org_bbox_coords.top
            bbox_info_contents[
                "width"
            ] = obj_meta.tracker_bbox_info.org_bbox_coords.width

            obj_meta_contents["tracker_bbox_info"] = bbox_info_contents

            l_classifier = obj_meta.classifier_meta_list
            classifier_list: List = list()
            while l_classifier is not None:
                try:
                    class_meta = pyds.NvDsClassifierMeta.cast(l_classifier.data)
                except StopIteration:
                    break
                classifier_meta_contents: Dict = dict()
                classifier_meta_contents[
                    "classifier_id"
                ] = class_meta.unique_component_id

                l_label_info = class_meta.label_info_list
                label_info_list: List = list()
                while l_label_info is not None:
                    try:
                        label_info_meta = pyds.NvDsLabelInfo.cast(l_label_info.data)
                    except StopIteration:
                        break
                    label_info_contents: Dict = dict()
                    label_info_contents["result_prob"] = label_info_meta.result_prob
                    label_info_contents["result_label"] = label_info_meta.result_label
                    label_info_contents[
                        "result_class_id"
                    ] = label_info_meta.result_class_id

                    label_info_list.append(label_info_contents)
                    try:
                        l_label_info = l_label_info.next
                    except StopIteration:
                        break

                classifier_meta_contents["label_info_list"] = label_info_list
                classifier_list.append(classifier_meta_contents)
                try:
                    l_classifier = l_classifier.next
                except StopIteration:
                    break

            obj_meta_contents["classifier_list"] = classifier_list
            obj_list.append(obj_meta_contents)
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        frame_meta_contents["obj_list"] = obj_list
        frame_list.append(frame_meta_contents)
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    msg["frame_list"] = frame_list

    return msg


def tiler_sink_pad_buffer_probe(pad, info, u_data):
    msg: Dict = dict()
    gst_buffer = info.get_buffer()

    parsed_msg = parse_buffer2msg(gst_buffer, msg)
    obj_list = parsed_msg["obj_list"]

    for obj_info in obj_list:
        PgieObj(obj_info)
        # obj_result = retrieve_pgie_obj(obj_list[i_obj])
        

    # print("msg", msg)
    return Gst.PadProbeReturn.OK


def draw_bounding_boxes(image, obj_meta, confidence):
    confidence = "{0:.2f}".format(confidence)
    rect_params = obj_meta.rect_params
    top = int(rect_params.top)
    left = int(rect_params.left)
    width = int(rect_params.width)
    height = int(rect_params.height)
    obj_name = pgie_classes_str[obj_meta.class_id]
    # image = cv2.rectangle(image, (left, top), (left + width, top + height), (0, 0, 255, 0), 2, cv2.LINE_4)
    color = (0, 0, 255, 0)
    w_percents = int(width * 0.05) if width > 100 else int(width * 0.1)
    h_percents = int(height * 0.05) if height > 100 else int(height * 0.1)
    linetop_c1 = (left + w_percents, top)
    linetop_c2 = (left + width - w_percents, top)
    image = cv2.line(image, linetop_c1, linetop_c2, color, 6)
    linebot_c1 = (left + w_percents, top + height)
    linebot_c2 = (left + width - w_percents, top + height)
    image = cv2.line(image, linebot_c1, linebot_c2, color, 6)
    lineleft_c1 = (left, top + h_percents)
    lineleft_c2 = (left, top + height - h_percents)
    image = cv2.line(image, lineleft_c1, lineleft_c2, color, 6)
    lineright_c1 = (left + width, top + h_percents)
    lineright_c2 = (left + width, top + height - h_percents)
    image = cv2.line(image, lineright_c1, lineright_c2, color, 6)
    # Note that on some systems cv2.putText erroneously draws horizontal lines across the image
    image = cv2.putText(
        image,
        obj_name + ",C=" + str(confidence),
        (left - 10, top - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (0, 0, 255, 0),
        2,
    )
    return image
