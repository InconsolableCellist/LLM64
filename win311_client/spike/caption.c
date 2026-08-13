/*
 * Tier 2 spike: a demo harness for the 3.1 chrome
 *
 * The chrome itself now lives in src/chrome.c, because main.c wants it
 * too. What is left here is the smallest program that puts a chromed
 * window on screen: no proxy, no MDI, no documents. It exists so the
 * chrome can be screenshotted and pixel-diffed against a real 3.11
 * capture without dragging the whole client into the loop, and so a
 * change to chrome.c can be checked in seconds rather than by building
 * the application.
 *
 *   make spike     -> build/CAPSPIKE.EXE   32-bit PE
 *   make spike16   -> build/CAPSPK16.EXE   16-bit NE, runs on real 3.11
 *   tools/pixdiff.py <capture.png>         measures it
 *
 * The two builds render 0 differing pixels out of 38,798.
 */

#include <windows.h>
#include <string.h>
#include "llmport.h"
#include "chrome.h"

/* Stand-ins for llm64.rc's menu bar. The real client hands chrome_init
   its own LoadMenu() result instead; these only have to prove that a
   plain HMENU is all the module wants. */
static const char *TITLES[] = {
    "&File", "&Link", "&Settings", "&Window", "&Help"
};
#define NTITLES ((int)(sizeof(TITLES) / sizeof(TITLES[0])))

static HMENU demo_menu(void)
{
    HMENU bar = CreateMenu();
    int i;

    for (i = 0; i < NTITLES; i++) {
        HMENU pop = CreatePopupMenu();

        AppendMenu(pop, MF_STRING, 201, "&First item");
        AppendMenu(pop, MF_STRING, 202, "Second &item");
        AppendMenu(pop, MF_SEPARATOR, 0, NULL);
        AppendMenu(pop, MF_STRING, 203, "A&nother");
        AppendMenu(bar, MF_POPUP, (UINT)pop, TITLES[i]);
    }
    return bar;
}

static void demo_paint(HWND hwnd, HDC hdc)
{
    RECT rc, r;
    HFONT of;
    int e = chrome_edge(hwnd);

    GetClientRect(hwnd, &rc);
    r.left = e;
    r.right = rc.right - e;
    r.top = chrome_top(hwnd);
    r.bottom = rc.bottom - e;
    {
        HBRUSH w = CreateSolidBrush(RGB(0xFF, 0xFF, 0xFF));

        FillRect(hdc, &r, w);
        DeleteObject(w);
    }
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, RGB(0, 0, 0));
    of = SelectObject(hdc, GetStockObject(SYSTEM_FIXED_FONT));
    InflateRect(&r, -8, -8);
    DrawText(hdc,
             "Tier 2 spike.\r\n\r\n"
             "This frame is drawn by the program, not by Windows, and\r\n"
             "every metric in it was measured off a real 3.11 capture\r\n"
             "with a pixel differ rather than remembered.\r\n\r\n"
             "Drag the caption. Drag any edge or corner to resize - the\r\n"
             "corner grips are 22 px, as 3.1's are, and the ticks in the\r\n"
             "border show you where they end.\r\n\r\n"
             "The menu bar is ours too: white, as 3.1's is, not the gray\r\n"
             "95 changed it to. The popups are real HMENUs handed to\r\n"
             "TrackPopupMenu, so arrow keys and mnemonics still work.",
             -1, &r, DT_LEFT | DT_TOP | DT_NOPREFIX);
    SelectObject(hdc, of);
}

long FAR PASCAL _export WndProc(HWND hwnd, UINT msg, UINT wParam,
                                LONG lParam)
{
    PAINTSTRUCT ps;
    LONG r;

    /* One line, before anything else. Everything the frame owns is in
       here; anything it does not own falls through untouched. */
    if (chrome_msg(hwnd, msg, wParam, lParam, &r))
        return r;

    switch (msg) {
    case WM_CREATE:
        chrome_init(hwnd, demo_menu());
        return 0;

    case WM_PAINT:
        if (IsIconic(hwnd))
            break;      /* an iconic window is DefWindowProc's to draw */
        BeginPaint(hwnd, &ps);
        chrome_paint(hwnd, ps.hdc);
        demo_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;

    case WM_SIZE:
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

int PASCAL WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASS wc;
    HWND hwnd;
    MSG msg;

    (void)prev;

    memset(&wc, 0, sizeof(wc));
    wc.style         = CHROME_CLASS_STYLE;
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursor(NULL, IDC_ARROW);
    wc.hIcon         = LoadIcon(NULL, IDI_APPLICATION);
    wc.hbrBackground = NULL;        /* we cover every pixel ourselves */
    wc.lpszClassName = "LLM64CapSpike";
    if (!RegisterClass(&wc))
        return 1;

    hwnd = CreateWindow("LLM64CapSpike", "LLM64", CHROME_STYLE,
                        CW_USEDEFAULT, CW_USEDEFAULT, 640, 300,
                        NULL, NULL, inst, NULL);
    if (!hwnd)
        return 1;
    /* "max" starts maximised, which is the state the reference capture is
       in - the only way to compare caption geometry with no border in the
       way. */
    ShowWindow(hwnd, (cmd && strstr(cmd, "max")) ? SW_MAXIMIZE : show);
    UpdateWindow(hwnd);

    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    return (int)msg.wParam;
}
