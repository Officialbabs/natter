/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * nattercamfix: a GStreamer tracer that unfreezes the camera in WebKitGTK on GStreamer >= 1.28.
 *
 * Why: WebKit's camera capture pipeline runs on the system clock with base time 0, so a camera's
 * buffers carry absolute CLOCK_MONOTONIC timestamps (hours). Its "videorate" element, left at its
 * defaults, fills the gap from the segment start (0) up to the first real frame by repeating that
 * frame: millions of copies, so the page shows one frozen frame for a minute or more. WebKit set
 * drop-only and average-period=1 on that videorate until it gated them off for GStreamer >= 1.28
 * (WebKit 309310@main, in every 2.52.x and 2.54.x). WebKit bug 324582 and PR 74373 (skip-to-first)
 * fix it upstream; this tracer applies the same settings until that lands where Natter runs.
 *
 * What: on "bin-add-post", once the element sits inside a pipeline WebKit's capturer made (named
 * "<Kind>_capturer_pipeline<N>_<address>" by GStreamerCapturer::makeElement), every element of
 * factory "videorate" in it gets skip-to-first=TRUE, drop-only=TRUE and average-period=1. Nothing
 * else is touched; elsewhere the tracer only looks. One INFO line (category nattercamfix) per
 * videorate it changes.
 *
 * Use: Natter (natter/gstfix.py) puts the folder holding libnattercamfix.so first in
 * GST_PLUGIN_PATH(_1_0) and "nattercamfix" in GST_TRACERS before WebKit starts; WebKit's sandbox
 * passes both on to its web processes. Native installs with GStreamer >= 1.28 only.
 *
 * Build: install.sh and meson.build do it; by hand:
 *   cc -shared -fPIC -O2 -Wall -o libnattercamfix.so nattercamfix.c \
 *       $(pkg-config --cflags --libs gstreamer-1.0)
 * Check: GST_TRACERS=nattercamfix GST_DEBUG=nattercamfix:4 GST_PLUGIN_PATH=<its folder> ...
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

#define NATTER_TYPE_CAM_FIX (natter_cam_fix_get_type ())
G_DECLARE_FINAL_TYPE (NatterCamFix, natter_cam_fix, NATTER, CAM_FIX, GstTracer)

struct _NatterCamFix
{
  GstTracer parent;
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
  GST_DEBUG ("watching for WebKit's camera videorate");
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  GST_DEBUG_CATEGORY_INIT (natter_cam_fix_debug, "nattercamfix", 0,
      "Natter: settings for WebKit's camera videorate");
  return gst_tracer_register (plugin, "nattercamfix", NATTER_TYPE_CAM_FIX);
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR, nattercamfix,
    "Natter: unfreezes WebKit's camera capture on GStreamer 1.28 and newer", plugin_init,
    NATTER_VERSION, "GPL", "Natter", "https://github.com/Officialbabs/natter")
