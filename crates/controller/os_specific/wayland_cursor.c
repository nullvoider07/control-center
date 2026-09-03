/* Read the pointer position from a PipeWire screencast stream's cursor metadata.
 *
 * Wayland does not let a client ask where the pointer is, and XWayland only
 * observes it while it is over one of XWayland's own surfaces - over a native
 * Wayland window it repeats the last position it saw, forever and without error.
 * The compositor will report the position, but only to a screencast consumer,
 * and only when cursor_mode is METADATA.
 *
 * METADATA means the cursor is NOT drawn into the frames; its position is
 * attached to each buffer instead. This program reads that coordinate pair and
 * never inspects a pixel. The frames are dequeued and requeued untouched.
 *
 *   one-shot:  wayland_cursor <pipewire-fd> <node-id> [timeout-ms]
 *              prints "X Y" once, exit 0; exit 1 if no metadata arrived.
 *   follow:    CC_CURSOR_FOLLOW=1 wayland_cursor <fd> <node> <timeout-ms>
 *              prints "X Y" on every change until the timeout.
 *
 * Built on demand by wayland_portal.py; see CursorTracker there.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <spa/param/video/format-utils.h>
#include <spa/buffer/meta.h>
#include <spa/utils/result.h>
#include <pipewire/pipewire.h>

/* Not in the SPA headers; PipeWire defines it in its own examples. The cursor
 * meta is a fixed header optionally followed by a w*h BGRA bitmap. */
#define CURSOR_META_SIZE(w, h) (sizeof(struct spa_meta_cursor) + \
                                sizeof(struct spa_meta_bitmap) + \
                                (uint32_t)((w) * (h) * 4))

static int verbose;

struct state {
    struct pw_main_loop *loop;
    struct pw_context *context;
    struct pw_core *core;
    struct pw_stream *stream;
    struct spa_hook stream_listener;
    int found;
    int follow;
    int32_t x, y;
    int32_t last_x, last_y;
    uint64_t n_buffers, n_valid;
};

static void on_process(void *userdata)
{
    struct state *st = userdata;
    struct pw_buffer *b;
    struct spa_meta_cursor *mc;

    if ((b = pw_stream_dequeue_buffer(st->stream)) == NULL)
        return;
    st->n_buffers++;

    mc = spa_buffer_find_meta_data(b->buffer, SPA_META_Cursor, sizeof(*mc));
    if (mc != NULL && spa_meta_cursor_is_valid(mc)) {
        st->n_valid++;
        st->x = mc->position.x;
        st->y = mc->position.y;
        if (st->follow &&
            (!st->found || st->x != st->last_x || st->y != st->last_y)) {
            printf("%d %d\n", st->x, st->y);
            fflush(stdout);
            st->last_x = st->x;
            st->last_y = st->y;
        }
        st->found = 1;
    }
    pw_stream_queue_buffer(st->stream, b);

    if (st->found && !st->follow)
        pw_main_loop_quit(st->loop);
}

/* Meta is negotiated AFTER the format, not at connect time. ParamMeta passed to
 * pw_stream_connect is silently ignored: the stream runs, buffers arrive, and
 * the only metas attached are the server's own. */
static void on_param_changed(void *userdata, uint32_t id, const struct spa_pod *param)
{
    struct state *st = userdata;
    uint8_t buf[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof(buf));
    const struct spa_pod *params[3];

    if (id != SPA_PARAM_Format || param == NULL)
        return;

    params[0] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(8, 2, 16));
    params[1] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
        SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
    params[2] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        /* The maximum matters and it fails silently. mutter declares the cursor
         * meta at a FIXED CURSOR_META_SIZE(384,384) = 589872 bytes. PipeWire
         * intersects the two size ranges and, when they do not overlap,
         * allocates no meta at all - no error at any layer, the stream runs,
         * buffers arrive, and the cursor is simply absent. A 256x256 cap
         * reproduces that. 1024x1024 is what OBS asks for. */
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(
            CURSOR_META_SIZE(64, 64),
            CURSOR_META_SIZE(1, 1),
            CURSOR_META_SIZE(1024, 1024)));

    pw_stream_update_params(st->stream, params, 3);
}

static void on_state_changed(void *userdata, enum pw_stream_state old,
                             enum pw_stream_state s, const char *error)
{
    struct state *st = userdata;
    if (verbose)
        fprintf(stderr, "  state: %s -> %s%s%s\n",
                pw_stream_state_as_string(old), pw_stream_state_as_string(s),
                error ? " err=" : "", error ? error : "");
    if (s == PW_STREAM_STATE_ERROR) {
        fprintf(stderr, "stream error: %s\n", error ? error : "(none)");
        pw_main_loop_quit(st->loop);
    }
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
};

static void on_timeout(void *userdata, uint64_t expirations)
{
    struct state *st = userdata;
    (void)expirations;
    pw_main_loop_quit(st->loop);
}

int main(int argc, char *argv[])
{
    struct state st = {0};
    const struct spa_pod *params[1];
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    struct spa_rectangle size_def = SPA_RECTANGLE(320, 240);
    struct spa_rectangle size_min = SPA_RECTANGLE(1, 1);
    struct spa_rectangle size_max = SPA_RECTANGLE(8192, 8192);
    struct spa_fraction rate_def = SPA_FRACTION(30, 1);
    struct spa_fraction rate_min = SPA_FRACTION(0, 1);
    struct spa_fraction rate_max = SPA_FRACTION(240, 1);
    int fd, res, timeout_ms;
    uint32_t node_id;
    struct spa_source *timer;

    if (argc < 3) {
        fprintf(stderr, "usage: %s <pipewire-fd> <node-id> [timeout-ms]\n", argv[0]);
        return 2;
    }
    fd = atoi(argv[1]);
    node_id = (uint32_t)strtoul(argv[2], NULL, 10);
    timeout_ms = argc > 3 ? atoi(argv[3]) : 2000;
    verbose = getenv("CC_CURSOR_DEBUG") != NULL;
    st.follow = getenv("CC_CURSOR_FOLLOW") != NULL;

    pw_init(&argc, &argv);

    st.loop = pw_main_loop_new(NULL);
    if (st.loop == NULL) { fprintf(stderr, "no loop\n"); return 1; }
    st.context = pw_context_new(pw_main_loop_get_loop(st.loop), NULL, 0);
    if (st.context == NULL) { fprintf(stderr, "no context\n"); return 1; }
    st.core = pw_context_connect_fd(st.context, fd, NULL, 0);
    if (st.core == NULL) { fprintf(stderr, "connect_fd failed\n"); return 1; }

    st.stream = pw_stream_new(st.core, "cc-cursor",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Video",
                          PW_KEY_MEDIA_CATEGORY, "Capture",
                          PW_KEY_MEDIA_ROLE, "Screen", NULL));
    if (st.stream == NULL) { fprintf(stderr, "no stream\n"); return 1; }
    pw_stream_add_listener(st.stream, &st.stream_listener, &stream_events, &st);

    params[0] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType,       SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype,    SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,    SPA_POD_CHOICE_ENUM_Id(4,
                                        SPA_VIDEO_FORMAT_BGRx,
                                        SPA_VIDEO_FORMAT_RGBx,
                                        SPA_VIDEO_FORMAT_BGRA,
                                        SPA_VIDEO_FORMAT_RGBA),
        SPA_FORMAT_VIDEO_size,      SPA_POD_CHOICE_RANGE_Rectangle(
                                        &size_def, &size_min, &size_max),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_CHOICE_RANGE_Fraction(
                                        &rate_def, &rate_min, &rate_max));

    res = pw_stream_connect(st.stream, PW_DIRECTION_INPUT, node_id,
                            PW_STREAM_FLAG_AUTOCONNECT |
                            PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
    if (res < 0) {
        fprintf(stderr, "connect failed: %s\n", spa_strerror(res));
        return 1;
    }
    pw_stream_set_active(st.stream, true);

    if (timeout_ms > 0) {
        timer = pw_loop_add_timer(pw_main_loop_get_loop(st.loop), on_timeout, &st);
        {
            struct timespec ts = { .tv_sec = timeout_ms / 1000,
                                   .tv_nsec = (long)(timeout_ms % 1000) * 1000000L };
            pw_loop_update_timer(pw_main_loop_get_loop(st.loop), timer, &ts, NULL, false);
        }
    }

    pw_main_loop_run(st.loop);

    pw_stream_destroy(st.stream);
    pw_core_disconnect(st.core);
    pw_context_destroy(st.context);
    pw_main_loop_destroy(st.loop);
    pw_deinit();

    if (!st.found) {
        fprintf(stderr, "no cursor metadata within %dms\n", timeout_ms);
        return 1;
    }
    if (!st.follow)
        printf("%d %d\n", st.x, st.y);
    return 0;
}
