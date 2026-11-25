/*
 * fb_mirror.c — Low-CPU Linux framebuffer mirror for M5Stack Cardputer Zero
 *
 * Mirrors the built-in 1.9" display (/dev/fb_lcd -> /dev/fb0, 320x170 RGB565)
 * onto the secondary top CAP display (/dev/fb_cap -> /dev/fb1, 320x240 RGB565).
 *
 * Features:
 *   - Explicitly unblanks & activates the destination DRM framebuffer on start
 *     so panel-mipi-dbi initializes cleanly even after a live driver rebind.
 *   - Uses pwrite() to trigger immediate drm_fbdev_generic damage flushes.
 *   - Compares source frames with memcmp() — zero SPI0 traffic when idle.
 *   - Supports scaling modes:
 *       smooth  : RGB565 vertical linear interpolation (fills 320x240 smoothly)
 *       nearest : Fast nearest-neighbor vertical scale (fills 320x240)
 *       center  : 1:1 pixel-perfect centered (320x170 inside 320x240)
 *   - Clears destination screen cleanly on SIGTERM/SIGINT.
 *
 * Build:
 *   gcc -O2 -Wall -Wextra -o fb_mirror fb_mirror.c
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t g_running = 1;

static void handle_signal(int sig) {
    (void)sig;
    g_running = 0;
}

typedef enum {
    SCALE_SMOOTH = 0,
    SCALE_NEAREST = 1,
    SCALE_CENTER = 2,
} scale_mode_t;

static const char *resolve_fb(const char *explicit_path, const char *pref, const char *fallback) {
    if (explicit_path && explicit_path[0])
        return explicit_path;
    if (access(pref, R_OK) == 0)
        return pref;
    return fallback;
}

static int write_full_fb(int fd, const uint8_t *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t n = pwrite(fd, buf + off, len - off, (off_t)off);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        off += (size_t)n;
    }
    return 0;
}

static inline uint16_t blend_rgb565(uint16_t a, uint16_t b, uint32_t w) {
    /* w in [0..256]: 0 -> a, 256 -> b */
    if (w == 0)
        return a;
    if (w >= 256)
        return b;
    uint32_t inv = 256 - w;
    uint32_t r = (((a >> 11) & 0x1F) * inv + ((b >> 11) & 0x1F) * w + 128) >> 8;
    uint32_t g = (((a >> 5) & 0x3F) * inv + ((b >> 5) & 0x3F) * w + 128) >> 8;
    uint32_t bl = ((a & 0x1F) * inv + (b & 0x1F) * w + 128) >> 8;
    return (uint16_t)((r << 11) | (g << 5) | bl);
}

static void render_frame_rgb565(
    const uint8_t *src, uint32_t sw, uint32_t sh, uint32_t sstride,
    uint8_t *dst, uint32_t dw, uint32_t dh, uint32_t dstride,
    scale_mode_t mode,
    const uint32_t *y_idx0, const uint32_t *y_idx1, const uint32_t *y_weight,
    const uint32_t *x_map) {
    if (mode == SCALE_CENTER) {
        memset(dst, 0, (size_t)dstride * dh);
        uint32_t copy_w = (sw < dw) ? sw : dw;
        uint32_t copy_h = (sh < dh) ? sh : dh;
        uint32_t dst_x0 = (dw > copy_w) ? (dw - copy_w) / 2 : 0;
        uint32_t dst_y0 = (dh > copy_h) ? (dh - copy_h) / 2 : 0;
        uint32_t src_x0 = (sw > copy_w) ? (sw - copy_w) / 2 : 0;
        uint32_t src_y0 = (sh > copy_h) ? (sh - copy_h) / 2 : 0;
        size_t row_bytes = (size_t)copy_w * 2;
        for (uint32_t y = 0; y < copy_h; y++) {
            const uint8_t *srow = src + (size_t)(src_y0 + y) * sstride + (size_t)src_x0 * 2;
            uint8_t *drow = dst + (size_t)(dst_y0 + y) * dstride + (size_t)dst_x0 * 2;
            memcpy(drow, srow, row_bytes);
        }
        return;
    }

    int same_width = (sw == dw);
    for (uint32_t y = 0; y < dh; y++) {
        uint16_t *drow = (uint16_t *)(dst + (size_t)y * dstride);
        if (mode == SCALE_NEAREST || y_weight[y] == 0) {
            const uint16_t *srow = (const uint16_t *)(src + (size_t)y_idx0[y] * sstride);
            if (same_width) {
                memcpy(drow, srow, (size_t)dw * 2);
            } else {
                for (uint32_t x = 0; x < dw; x++)
                    drow[x] = srow[x_map[x]];
            }
        } else {
            const uint16_t *srow0 = (const uint16_t *)(src + (size_t)y_idx0[y] * sstride);
            const uint16_t *srow1 = (const uint16_t *)(src + (size_t)y_idx1[y] * sstride);
            uint32_t w = y_weight[y];
            if (same_width) {
                for (uint32_t x = 0; x < dw; x++)
                    drow[x] = blend_rgb565(srow0[x], srow1[x], w);
            } else {
                for (uint32_t x = 0; x < dw; x++) {
                    uint32_t sx = x_map[x];
                    drow[x] = blend_rgb565(srow0[sx], srow1[sx], w);
                }
            }
        }
    }
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s [options]\n"
            "  --src PATH      Source framebuffer (default: /dev/fb_lcd or /dev/fb0)\n"
            "  --dst PATH      Destination framebuffer (default: /dev/fb_cap or /dev/fb1)\n"
            "  --scale MODE    smooth (default) | nearest | center\n"
            "  --fps N         Max mirror refresh rate in Hz (default: 20)\n"
            "  --once          Mirror a single frame and exit\n",
            prog);
}

int main(int argc, char **argv) {
    const char *src_arg = NULL;
    const char *dst_arg = NULL;
    scale_mode_t mode = SCALE_SMOOTH;
    int fps = 20;
    int once = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--src") == 0 && i + 1 < argc) {
            src_arg = argv[++i];
        } else if (strcmp(argv[i], "--dst") == 0 && i + 1 < argc) {
            dst_arg = argv[++i];
        } else if (strcmp(argv[i], "--scale") == 0 && i + 1 < argc) {
            const char *m = argv[++i];
            if (strcmp(m, "smooth") == 0)
                mode = SCALE_SMOOTH;
            else if (strcmp(m, "nearest") == 0)
                mode = SCALE_NEAREST;
            else if (strcmp(m, "center") == 0)
                mode = SCALE_CENTER;
            else {
                fprintf(stderr, "[ERR] Unknown scale mode: %s\n", m);
                return 2;
            }
        } else if (strcmp(argv[i], "--fps") == 0 && i + 1 < argc) {
            fps = atoi(argv[++i]);
            if (fps < 1) fps = 1;
            if (fps > 60) fps = 60;
        } else if (strcmp(argv[i], "--once") == 0) {
            once = 1;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "[ERR] Unknown argument: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }

    const char *src_path = resolve_fb(src_arg, "/dev/fb_lcd", "/dev/fb0");
    const char *dst_path = resolve_fb(dst_arg, "/dev/fb_cap", "/dev/fb1");

    int src_fd = open(src_path, O_RDONLY);
    if (src_fd < 0) {
        fprintf(stderr, "[ERR] open(%s): %s\n", src_path, strerror(errno));
        return 1;
    }

    int dst_fd = open(dst_path, O_RDWR);
    if (dst_fd < 0) {
        fprintf(stderr, "[ERR] open(%s): %s\n", dst_path, strerror(errno));
        close(src_fd);
        return 1;
    }

    struct fb_var_screeninfo svar, dvar;
    struct fb_fix_screeninfo sfix, dfix;
    if (ioctl(src_fd, FBIOGET_VSCREENINFO, &svar) < 0 ||
        ioctl(src_fd, FBIOGET_FSCREENINFO, &sfix) < 0 ||
        ioctl(dst_fd, FBIOGET_VSCREENINFO, &dvar) < 0 ||
        ioctl(dst_fd, FBIOGET_FSCREENINFO, &dfix) < 0) {
        fprintf(stderr, "[ERR] Failed to query framebuffer info: %s\n", strerror(errno));
        close(dst_fd);
        close(src_fd);
        return 1;
    }

    if (svar.bits_per_pixel != 16 || dvar.bits_per_pixel != 16) {
        fprintf(stderr, "[ERR] Expected 16bpp RGB565 (got src=%ubpp, dst=%ubpp)\n",
                svar.bits_per_pixel, dvar.bits_per_pixel);
        close(dst_fd);
        close(src_fd);
        return 1;
    }

    /* Wake / unblank DRM pipeline on destination fb so panel-mipi-dbi runs init */
    (void)ioctl(dst_fd, FBIOBLANK, FB_BLANK_UNBLANK);
    dvar.activate = FB_ACTIVATE_NOW | FB_ACTIVATE_FORCE;
    (void)ioctl(dst_fd, FBIOPUT_VSCREENINFO, &dvar);

    size_t src_size = (size_t)sfix.line_length * svar.yres;
    size_t dst_size = (size_t)dfix.line_length * dvar.yres;

    void *src_map = mmap(NULL, src_size, PROT_READ, MAP_SHARED, src_fd, 0);
    if (src_map == MAP_FAILED) {
        fprintf(stderr, "[ERR] mmap(%s): %s\n", src_path, strerror(errno));
        close(dst_fd);
        close(src_fd);
        return 1;
    }

    uint8_t *prev_src = calloc(1, src_size);
    uint8_t *curr_src = malloc(src_size);
    uint8_t *dst_buf = calloc(1, dst_size);
    uint32_t *y_idx0 = calloc(dvar.yres, sizeof(uint32_t));
    uint32_t *y_idx1 = calloc(dvar.yres, sizeof(uint32_t));
    uint32_t *y_weight = calloc(dvar.yres, sizeof(uint32_t));
    uint32_t *x_map = calloc(dvar.xres, sizeof(uint32_t));

    if (!prev_src || !curr_src || !dst_buf || !y_idx0 || !y_idx1 || !y_weight || !x_map) {
        fprintf(stderr, "[ERR] Out of memory\n");
        return 1;
    }

    /* Precompute scaling lookup tables */
    for (uint32_t y = 0; y < dvar.yres; y++) {
        if (dvar.yres <= 1 || svar.yres <= 1) {
            y_idx0[y] = y_idx1[y] = 0;
            y_weight[y] = 0;
        } else {
            uint32_t fp = (uint32_t)(((uint64_t)y * (svar.yres - 1) * 256) / (dvar.yres - 1));
            uint32_t i0 = fp >> 8;
            uint32_t i1 = (i0 + 1 < svar.yres) ? (i0 + 1) : (svar.yres - 1);
            y_idx0[y] = i0;
            y_idx1[y] = i1;
            y_weight[y] = fp & 0xFF;
        }
    }
    for (uint32_t x = 0; x < dvar.xres; x++) {
        x_map[x] = (uint32_t)(((uint64_t)x * svar.xres) / dvar.xres);
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    fprintf(stdout, "[OK] fb_mirror: %s (%ux%u) -> %s (%ux%u), scale=%s, fps=%d\n",
            src_path, svar.xres, svar.yres, dst_path, dvar.xres, dvar.yres,
            mode == SCALE_SMOOTH ? "smooth" : (mode == SCALE_NEAREST ? "nearest" : "center"),
            fps);
    fflush(stdout);

    struct timespec sleep_ts;
    sleep_ts.tv_sec = 0;
    sleep_ts.tv_nsec = (1000000000L / fps);

    int first_frame = 1;
    while (g_running) {
        memcpy(curr_src, src_map, src_size);
        if (first_frame || memcmp(curr_src, prev_src, src_size) != 0) {
            render_frame_rgb565(curr_src, svar.xres, svar.yres, sfix.line_length,
                                dst_buf, dvar.xres, dvar.yres, dfix.line_length,
                                mode, y_idx0, y_idx1, y_weight, x_map);
            if (write_full_fb(dst_fd, dst_buf, dst_size) < 0) {
                fprintf(stderr, "[WARN] pwrite(%s): %s\n", dst_path, strerror(errno));
            }
            memcpy(prev_src, curr_src, src_size);
            first_frame = 0;
        }
        if (once)
            break;
        nanosleep(&sleep_ts, NULL);
    }

    if (!once) {
        memset(dst_buf, 0, dst_size);
        (void)write_full_fb(dst_fd, dst_buf, dst_size);
    }

    free(x_map);
    free(y_weight);
    free(y_idx1);
    free(y_idx0);
    free(dst_buf);
    free(curr_src);
    free(prev_src);
    munmap(src_map, src_size);
    close(dst_fd);
    close(src_fd);
    return 0;
}
