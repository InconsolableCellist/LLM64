/*
 * Render the 3.1 controls to a file, so they can be diffed rather than
 * admired.
 *
 * The caption was measured against a screenshot of a real 3.11 machine,
 * and so are the controls - the same capture has the Music window's
 * Play/Pause/Stop/Next in it, and the checkbox under the Picture
 * browser. What it does NOT have is a way to photograph our version
 * without a window manager, a screen grabber and a frame's worth of
 * offset arithmetic in between, every one of which has been wrong at
 * least once in this project.
 *
 * So this draws into a memory DC and writes a BMP. No window, no
 * message loop, no display. Run it and diff the file:
 *
 *     make ctlspike
 *     wine build/CTLSPIKE.EXE build/ctl.bmp
 *     tools/ctldiff.py build/ctl.bmp
 *
 * Win32 only, deliberately. chrome.c is already proven to render
 * identically on both targets - 0 pixels different out of 38,798 - so
 * one of them is enough to measure, and the 32-bit one runs without a
 * DOS emulator in the way.
 */

#include <windows.h>
#include <stdio.h>
#include "llmport.h"
#include "chrome.h"

#define W 200
#define H 180

/* Where each shape goes. tools/ctldiff.py knows these, and the two must
   agree or the diff quietly compares the wrong thing. */
#define BTN_X    10
#define BTN_Y    10
#define BTN_W    121    /* measured: the Pause button, x=643..763 */
#define BTN_H    25     /*           y=716..740                   */
#define GAP      10
#define CHK_X    10
#define CHK_Y    (BTN_Y + 3 * (BTN_H + GAP))
/* A scrollbar tall enough to hold both arrows and a thumb between. */
#define SB_X     150
#define SB_Y     10
#define SB_H     150

static int save_bmp(HBITMAP bmp, const char *path)
{
    BITMAPFILEHEADER fh;
    BITMAPINFO bi;
    HDC dc;
    FILE *f;
    unsigned char *bits;
    long stride = ((long)W * 3 + 3) & ~3L;
    long size = stride * H;
    int ok;

    bits = (unsigned char *)malloc((size_t)size);
    if (!bits)
        return 0;
    memset(&bi, 0, sizeof(bi));
    bi.bmiHeader.biSize = sizeof(bi.bmiHeader);
    bi.bmiHeader.biWidth = W;
    bi.bmiHeader.biHeight = H;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 24;
    bi.bmiHeader.biCompression = BI_RGB;

    dc = GetDC(NULL);
    ok = GetDIBits(dc, bmp, 0, H, bits, &bi, DIB_RGB_COLORS) != 0;
    ReleaseDC(NULL, dc);
    if (!ok) {
        free(bits);
        return 0;
    }

    memset(&fh, 0, sizeof(fh));
    fh.bfType = 0x4D42;         /* "BM" */
    fh.bfOffBits = sizeof(fh) + sizeof(bi.bmiHeader);
    fh.bfSize = fh.bfOffBits + size;

    f = fopen(path, "wb");
    if (!f) {
        free(bits);
        return 0;
    }
    /* Field by field: BITMAPFILEHEADER is 14 bytes on the wire and
       sizeof() rounds it to 16 on a 32-bit compiler that packs to 4. */
    fwrite(&fh.bfType, 2, 1, f);
    fwrite(&fh.bfSize, 4, 1, f);
    fwrite(&fh.bfReserved1, 2, 1, f);
    fwrite(&fh.bfReserved2, 2, 1, f);
    fwrite(&fh.bfOffBits, 4, 1, f);
    fwrite(&bi.bmiHeader, sizeof(bi.bmiHeader), 1, f);
    fwrite(bits, 1, (size_t)size, f);
    fclose(f);
    free(bits);
    return 1;
}

int PASCAL WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    HDC screen, dc;
    HBITMAP bmp, old;
    HBRUSH face;
    RECT r;
    const char *path = (cmd && *cmd) ? cmd : "ctl.bmp";
    int i;

    (void)inst; (void)prev; (void)show;

    screen = GetDC(NULL);
    dc = CreateCompatibleDC(screen);
    bmp = CreateCompatibleBitmap(screen, W, H);
    ReleaseDC(NULL, screen);
    if (!dc || !bmp)
        return 1;
    old = SelectObject(dc, bmp);

    /* Face grey behind everything, because that is what a 3.1 button
       sits on and what shows through its knocked-out corners. */
    face = CreateSolidBrush(RGB(0xC0, 0xC0, 0xC0));
    SetRect(&r, 0, 0, W, H);
    FillRect(dc, &r, face);
    DeleteObject(face);

    for (i = 0; i < 3; i++) {
        SetRect(&r, BTN_X, BTN_Y + i * (BTN_H + GAP),
                BTN_X + BTN_W, BTN_Y + i * (BTN_H + GAP) + BTN_H);
        /* raised, pressed, default */
        chrome_button_face(dc, &r, i == 1, i == 2);
    }
    chrome_checkbox_face(dc, CHK_X, CHK_Y, 1, 0);
    chrome_checkbox_face(dc, CHK_X + 20, CHK_Y, 0, 0);
    /* The scrollbar draws through a real window, so it needs one. Off
       screen and never shown: the pixels are what is being measured,
       and WM_PAINT does not care whether anybody could have seen it. */
    {
        HWND sb;

        chrome_scrollbar_init(inst);
        sb = CreateWindow("LLM64Scroll", NULL, WS_POPUP,
                          -2000, -2000, CHROME_SB_W, SB_H,
                          NULL, NULL, inst, NULL);
        if (sb) {
            chrome_scrollbar_set(sb, 40, 100, 30);
            /* It draws at its own origin, so move the DC's instead. */
            SetViewportOrgEx(dc, SB_X, SB_Y, NULL);
            SendMessage(sb, WM_PAINT, (WPARAM)dc, 0L);
            SetViewportOrgEx(dc, 0, 0, NULL);
            DestroyWindow(sb);
        }
    }

    SelectObject(dc, old);
    if (!save_bmp(bmp, path))
        return 1;
    DeleteObject(bmp);
    DeleteDC(dc);
    return 0;
}
