/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * nattercamfix: a GStreamer tracer with two fixes for WebKitGTK: it unfreezes the camera on
 * GStreamer >= 1.28, and it stops Web Audio's sound clicking after silence on WebKit < 2.54.2.
 *
 * The camera. Why: WebKit's camera capture pipeline runs on the system clock with base time 0, so
 * a camera's buffers carry absolute CLOCK_MONOTONIC timestamps (hours). Its "videorate" element,
 * left at its defaults, fills the gap from the segment start (0) up to the first real frame by
 * repeating that frame: millions of copies, so the page shows one frozen frame for a minute or
 * more. WebKit set drop-only and average-period=1 on that videorate until it gated them off for
 * GStreamer >= 1.28 (WebKit 309310@main, in every 2.52.x and 2.54.x). WebKit bug 324582 and PR
 * 74373 (skip-to-first) fix it upstream; this tracer applies the same settings until that lands
 * where Natter runs.
 * What: on "bin-add-post", once the element sits inside a pipeline WebKit's capturer made (named
 * "<Kind>_capturer_pipeline<N>_<address>" by GStreamerCapturer::makeElement), every element of
 * factory "videorate" in it gets skip-to-first=TRUE, drop-only=TRUE and average-period=1. One INFO
 * line (category nattercamfix) per videorate it changes.
 *
 * Web Audio. Why: WebKit's Web Audio source (WebKitWebAudioSrc, a bin around an appsrc, made with
 * g_object_new, so it has no factory) marks each block it renders silent with GST_BUFFER_FLAG_GAP
 * and GST_BUFFER_FLAG_DROPPABLE. The buffer holds zeros all the same, but pulsesink skips gap
 * buffers, underruns and clicks when sound starts again: WhatsApp calls play through Web Audio.
 * WebKit's OpenMAX quirk cleared both flags for the same reason, and WebKit bug 326494 (2.54.2)
 * stopped setting them.
 * What: the first time an element of type "WebKitWebAudioSrc" joins a bin ("bin-add-post"), its
 * type is noted; from then on "pad-push-pre" and "pad-push-list-pre" clear both flags on what that
 * element's src pad (its ghost pad) pushes. Other pads cost a pointer read and a compare. A WebKit
 * that no longer sets the flags leaves it nothing to do. LOG lines (category nattercamfix) count
 * the buffers it clears.
 *
 * Elsewhere the tracer only looks.
 *
 * Use: Natter (natter/gstfix.py) puts the folder holding libnattercamfix.so first in
 * GST_PLUGIN_PATH(_1_0) and "nattercamfix" in GST_TRACERS before WebKit starts; WebKit's sandbox
 * passes both on to its web processes. Native installs with GStreamer >= 1.28 only.
 *
 * Build: install.sh and meson.build do it; by hand:
 *   cc -shared -fPIC -O2 -Wall -o libnattercamfix.so nattercamfix.c \
 *       $(pkg-config --cflags --libs gstreamer-1.0)
 * Check: GST_TRACERS=nattercamfix GST_DEBUG=nattercamfix:4 GST_PLUGIN_PATH=<its folder> ...
 *   (nattercamfix:6 adds a line per Web Audio buffer cleared)
 */
#include <string.h>

#include <gst/gst.h>

#ifndef NATTER_VERSION
#define NATTER_VERSION "0.1.0"
#endif
#ifndef PACKAGE
#define PACKAGE "natter"   /* GST_PLUGIN_DEFINE wants it: the plugin's source module */
#endif

GST_DEBUG_CATEGORY_STATIC (natter_cam_fix_debug);
#define GST_CAT_DEFAULT natter_cam_fix_debug

/* What WebKit's GStreamerCapturer::makeElement puts in its pipeline's name. */
#define CAPTURE_PIPELINE_MARK "_capturer_pipeline"
/* The type of WebKit's Web Audio source (WebKitWebAudioSourceGStreamer.cpp). */
#define WEB_AUDIO_SRC_TYPE "WebKitWebAudioSrc"

#define NATTER_TYPE_CAM_FIX (natter_cam_fix_get_type ())
G_DECLARE_FINAL_TYPE (NatterCamFix, natter_cam_fix, NATTER, CAM_FIX, GstTracer)

struct _NatterCamFix
{
  GstTracer parent;
  /* WebKitWebAudioSrc's type, once one has joined a bin; 0 until then. WebKit registers it when
   * it makes the first one, after this tracer starts. Set once, before that element streams. */
  GType web_audio_src;
};

G_DEFINE_TYPE (NatterCamFix, natter_cam_fix, GST_TYPE_TRACER)

/* The name of the pipeline `object` is in (or is), if WebKit's capturer made it; else NULL.
 * Free with g_free. */
static gchar *
capture_pipeline_name (GstObject * object)
{
  GstObject *top = gst_object_ref (object), *parent;
  gchar *name = NULL;

  while ((parent = gst_object_get_parent (top)) != NULL) {
    gst_object_unref (top);
    top = parent;
  }
  if (GST_IS_PIPELINE (top)) {
    name = gst_object_get_name (top);
    if (name == NULL || strstr (name, CAPTURE_PIPELINE_MARK) == NULL)
      g_clear_pointer (&name, g_free);
  }
  gst_object_unref (top);
  return name;
}

static gboolean
is_videorate (GstElement * element)
{
  GstElementFactory *factory = gst_element_get_factory (element);

  return factory != NULL
      && g_strcmp0 (gst_plugin_feature_get_name (GST_PLUGIN_FEATURE (factory)), "videorate") == 0;
}

/* Gives one videorate the settings; quiet when it already has them. */
static void
fix_videorate (GstElement * videorate, const gchar * pipeline)
{
  GObjectClass *klass = G_OBJECT_GET_CLASS (videorate);
  gboolean skip = FALSE, drop = FALSE;
  guint64 period = 0;

  /* All three exist since GStreamer 1.16; Natter only loads this on 1.28 and newer. */
  if (!g_object_class_find_property (klass, "skip-to-first")
      || !g_object_class_find_property (klass, "drop-only")
      || !g_object_class_find_property (klass, "average-period"))
    return;
  g_object_get (videorate, "skip-to-first", &skip, "drop-only", &drop,
      "average-period", &period, NULL);
  if (skip && drop && period == 1)
    return;
  g_object_set (videorate, "skip-to-first", TRUE, "drop-only", TRUE,
      "average-period", (guint64) 1, NULL);
  GST_INFO_OBJECT (videorate, "in %s: set skip-to-first=true, drop-only=true, average-period=1 "
      "(were %s, %s, %" G_GUINT64_FORMAT ")", pipeline, skip ? "true" : "false",
      drop ? "true" : "false", period);
}

/* Every videorate inside `bin`, at any depth. */
static void
fix_videorates_in (GstBin * bin, const gchar * pipeline)
{
  GstIterator *it = gst_bin_iterate_recurse (bin);
  GValue item = G_VALUE_INIT;
  gboolean done = FALSE;

  while (!done) {
    switch (gst_iterator_next (it, &item)) {
      case GST_ITERATOR_OK:
        if (is_videorate (g_value_get_object (&item)))
          fix_videorate (g_value_get_object (&item), pipeline);
        g_value_reset (&item);
        break;
      case GST_ITERATOR_RESYNC:      /* the bin changed: start over (fixing again is a no-op) */
        gst_iterator_resync (it);
        break;
      default:                 /* GST_ITERATOR_DONE or GST_ITERATOR_ERROR */
        done = TRUE;
        break;
    }
  }
  g_value_unset (&item);
  gst_iterator_free (it);
}

/* Clears the gap marks on one buffer from WebKit's Web Audio source. Its data is silence anyway.
 * In place, as WebKit's own OpenMAX quirk did, even when the buffer isn't writable: a copy would
 * cost a buffer per block, and this hook can't hand one on. Nothing else holds these buffers here
 * (the appsrc passes on its only reference), and clearing GAP only takes away a hint that the
 * data may be skipped, so whoever else might hold one would still see the same silence. */
static inline void
clear_gap (GstPad * pad, GstBuffer * buffer)
{
  if (!GST_BUFFER_FLAG_IS_SET (buffer, GST_BUFFER_FLAG_GAP)
      && !GST_BUFFER_FLAG_IS_SET (buffer, GST_BUFFER_FLAG_DROPPABLE))
    return;
  GST_BUFFER_FLAG_UNSET (buffer, GST_BUFFER_FLAG_GAP);
  GST_BUFFER_FLAG_UNSET (buffer, GST_BUFFER_FLAG_DROPPABLE);
  GST_LOG_OBJECT (pad, "cleared gap and droppable on a silent buffer, pts %" GST_TIME_FORMAT,
      GST_TIME_ARGS (GST_BUFFER_PTS (buffer)));
}

/* Whether `pad` belongs to WebKit's Web Audio source: its only pad is its src ghost pad. Runs on
 * every push, so a pointer read and a compare. */
static inline gboolean
is_web_audio_src_pad (GObject * self, GstPad * pad)
{
  GstObject *parent = GST_OBJECT_PARENT (pad);

  return parent != NULL && G_OBJECT_TYPE (parent) == ((NatterCamFix *) self)->web_audio_src;
}

static void
pad_push_pre (GObject * self, GstClockTime ts, GstPad * pad, GstBuffer * buffer)
{
  if (G_LIKELY (!is_web_audio_src_pad (self, pad)) || buffer == NULL)
    return;
  clear_gap (pad, buffer);
}

static void
pad_push_list_pre (GObject * self, GstClockTime ts, GstPad * pad, GstBufferList * list)
{
  guint i, n;

  if (G_LIKELY (!is_web_audio_src_pad (self, pad)) || list == NULL)
    return;
  n = gst_buffer_list_length (list);
  for (i = 0; i < n; i++)
    clear_gap (pad, gst_buffer_list_get (list, i));
}

/* Notes WebKitWebAudioSrc's type the first time one joins a bin (AudioDestinationGStreamer adds
 * it to its pipeline before it streams). */
static void
note_web_audio_src (NatterCamFix * self, GstElement * element)
{
  if (G_LIKELY (self->web_audio_src != 0)
      || strcmp (G_OBJECT_TYPE_NAME (element), WEB_AUDIO_SRC_TYPE) != 0)
    return;
  self->web_audio_src = G_OBJECT_TYPE (element);
  GST_INFO_OBJECT (element, "found WebKit's Web Audio source: clearing its gap marks");
}

/* gst_bin_add() returned. WebKit builds its converter (videoconvert ! videorate) in a bin of its
 * own and adds that bin to the capture pipeline afterwards, so the videorate is usually found when
 * its bin joins the pipeline; one added straight into a capture pipeline is handled too. No lock
 * is held here. */
static void
bin_add_post (GObject * self, GstClockTime ts, GstBin * bin, GstElement * element,
    gboolean result)
{
  gchar *pipeline;

  if (!result || element == NULL)
    return;
  note_web_audio_src ((NatterCamFix *) self, element);
  pipeline = capture_pipeline_name (GST_OBJECT (bin));
  if (pipeline == NULL)
    return;
  if (GST_IS_BIN (element))
    fix_videorates_in (GST_BIN (element), pipeline);
  else if (is_videorate (element))
    fix_videorate (element, pipeline);
  g_free (pipeline);
}

static void
natter_cam_fix_class_init (NatterCamFixClass * klass)
{
}

static void
natter_cam_fix_init (NatterCamFix * self)
{
  gst_tracing_register_hook (GST_TRACER (self), "bin-add-post", G_CALLBACK (bin_add_post));
  gst_tracing_register_hook (GST_TRACER (self), "pad-push-pre", G_CALLBACK (pad_push_pre));
  gst_tracing_register_hook (GST_TRACER (self), "pad-push-list-pre",
      G_CALLBACK (pad_push_list_pre));
  GST_DEBUG ("watching for WebKit's camera videorate and Web Audio source");
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  GST_DEBUG_CATEGORY_INIT (natter_cam_fix_debug, "nattercamfix", 0,
      "Natter: settings for WebKit's camera videorate, gap marks on its Web Audio");
  return gst_tracer_register (plugin, "nattercamfix", NATTER_TYPE_CAM_FIX);
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR, nattercamfix,
    "Natter: unfreezes WebKit's camera capture on GStreamer 1.28 and newer and stops its Web Audio "
    "clicking after silence", plugin_init,
    NATTER_VERSION, "GPL", "Natter", "https://github.com/Officialbabs/natter")
