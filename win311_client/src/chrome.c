/* LLM64 for Windows - the 3.1 window frame (see include/chrome.h) */

#include <windows.h>
#include <string.h>
#include "llmport.h"
#include "chrome.h"

#define CAP_H       18      /* caption, measured */
#define BTN_W       18      /* measured: square, the caption height */
/* The sizing border, measured off File Manager on real 3.11: one black
   line, TWO rows of flat C0C0C0, one black line. Four pixels, and no
   bevel anywhere in it - the 3D look everyone remembers from 3.1 is the
   buttons, not the frame. Drawing it raised was wrong and obvious the
   moment a real window sat beside it. */
#define FRAME       4

/* How far in from each outer corner the border carries a black tick.
   Measured at 22 px on all four sides. It is not decoration: it marks
   where the corner-resize zone ends and the edge-resize zone begins, so
   the hit-test below uses the same number. */
#define CORNER      22

/* A maximised 3.1 window has NO border - it is flush to the screen, which
   is why the reference capture's sysmenu box starts at x=0. Getting this
   wrong puts a grey margin around the caption and is the first thing that
   reads as not-quite-right. */
#define FRAME_W()   (g_maxed ? 0 : FRAME)
#define RULE        1       /* the black line under the caption */
#define MENU_H      18      /* measured: same height as the caption */

/* 3.1's COLOR_MENU is WHITE. Windows 95 made it button-grey and every
   later "classic" theme kept grey, so a grey menu bar is a 1995 tell as
   loud as the caption buttons. Measured off the reference: rows y21..y38
   are pure #FFFFFF with a black rule under them. */
#define C_MENU      RGB(0xFF, 0xFF, 0xFF)
/* 3.1's COLOR_HIGHLIGHT. An open bar item and a selected popup item are
   navy with white text - NOT inverted to black, which is what this drew
   first and what reads wrong beside the real thing. */
#define C_HILITE    RGB(0x00, 0x00, 0x80)
#define C_HILITETX  RGB(0xFF, 0xFF, 0xFF)
#define C_GRAYTEXT  RGB(0x80, 0x80, 0x80)
#define MENU_PAD    8       /* per side, measured: "File" ink starts at x=9 */
/* Zero, not 3, even though the ink does start 3 rows into the bar:
   DT_TOP aligns the font's CELL to the rect, and the System font carries
   3 rows of internal leading above the ink. Setting this to the measured
   ink offset stacks the two and puts every menu title 3 rows too low. */
#define MENU_TOP    0

/* The 3.1 system palette. Hardcoded on purpose: GetSysColor() on a
   modern machine returns 2026 values (COLOR_BTNFACE is #F0F0F0, not
   #C0C0C0), and SetSysColors is global and not ours to touch. Owning
   these is the whole point. */
#define C_FACE      RGB(0xC0, 0xC0, 0xC0)
#define C_HILIGHT   RGB(0xFF, 0xFF, 0xFF)
#define C_SHADOW    RGB(0x80, 0x80, 0x80)
#define C_FRAME     RGB(0x00, 0x00, 0x00)
#define C_ACTIVE    RGB(0x00, 0x00, 0x80)   /* measured: exactly #000080 */
#define C_INACTIVE  RGB(0xC0, 0xC0, 0xC0)
#define C_ACTTEXT   RGB(0xFF, 0xFF, 0xFF)
#define C_INACTTEXT RGB(0x00, 0x00, 0x00)
#define C_CLIENT    RGB(0xFF, 0xFF, 0xFF)

/* Hit codes for our own buttons. Anything the OS should handle (drag,
   resize) gets a real HT* code back instead and DefWindowProc does the
   work - we are replacing the painting, not the window management. */
#define HIT_NONE    0
#define HIT_SYS     1
#define HIT_MIN     2
#define HIT_MAX     3


static int  g_down = HIT_NONE;      /* button held, for the pressed bevel */
static int  g_maxed = 0;

/* The bar's titles and popups are read out of the application's own menu
   resource in chrome_init, so llm64.rc stays the single source of truth
   and every command id on the far side is unchanged. The popups are real
   HMENUs, so TrackPopupMenu runs them: keyboard navigation, mnemonics,
   highlight tracking and dismissal all remain the system's. We draw the
   bar; Windows still does the hard part. */
#define NMENUS  CHROME_MAX_MENUS
static char  g_label[CHROME_MAX_MENUS][32];
static HMENU g_pop[CHROME_MAX_MENUS];
static int   g_nmenus;
static int   g_open = -1;       /* bar item with its popup showing */
/* Which bar item the mouse went down on. The popup is opened on the UP,
   not the DOWN: TrackPopupMenu with TPM_LEFTBUTTON, called while the
   button is still physically held, sees the release that follows and
   dismisses itself. Under Wine that was survivable; on real 3.11 the
   menu appears for one frame, half drawn, and vanishes. The sysmenu box
   never had the bug because caption buttons always acted on the UP. */
static int   g_bar_down = -1;

/* Set from WM_NCACTIVATE rather than asked of GetActiveWindow at paint
   time. GetActiveWindow answers for the calling thread's queue, which is
   not the same question, and the two disagree exactly when another
   application has the focus - so the caption paints active while a real
   3.1 window beside it paints inactive. */
static int   g_active = 1;

/* The MDI client, if the frame has one. Needed because a MAXIMISED child
   has no caption of its own: 3.1 moves its sysmenu box to the left of the
   menu bar and its restore button to the right, and only the MDI client
   can say which child that is. Measured off a real 3.11 client. */
static HWND  g_mdi;

/* A caption button being held. 3.1 shows the button depressed for as long
   as the mouse is down and acts on the RELEASE, and only if the pointer is
   still on it - acting on the press instead is both wrong and unforgiving,
   since there is no way to change your mind. */
static HWND  g_cdown_wnd;
static int   g_cdown_hit;

/* The maximised child, or NULL. IsZoomed rather than WM_MDIGETACTIVE's
   maximised flag, because that flag is reported differently on the two
   targets and this is not. */
static HWND maxchild(void)
{
    HWND c;

    if (!g_mdi)
        return NULL;
    c = LLM_HWND(SendMessage(g_mdi, WM_MDIGETACTIVE, 0, 0L));
    return (c && IsZoomed(c)) ? c : NULL;
}

/* ------------------------------------------------------------------ */

/* A 3.1 bevel: highlight on top and left, shadow on bottom and right.
   `out` raises the surface, !out sinks it, which is all a pressed button
   is. */
/* Drawing primitives. Each one reproduces a structure read straight off
   the reference capture with a pixel differ, not from memory. Every
   offset below is measured. */

static void fill(HDC hdc, const RECT *r, COLORREF c)
{
    HBRUSH b = CreateSolidBrush(c);

    FillRect(hdc, r, b);
    DeleteObject(b);
}

/* Inclusive on both ends, because that is how the measured maps read. */
static void hline(HDC hdc, int x0, int x1, int y, COLORREF c)
{
    RECT r;

    r.left = x0; r.right = x1 + 1; r.top = y; r.bottom = y + 1;
    fill(hdc, &r, c);
}

static void vline(HDC hdc, int x, int y0, int y1, COLORREF c)
{
    RECT r;

    r.left = x; r.right = x + 1; r.top = y0; r.bottom = y1 + 1;
    fill(hdc, &r, c);
}

/* A 3.1 caption button: flat face, a ONE px highlight along the top and
   left, and a TWO px shadow along the right and bottom.
 *
 * The shadow is drawn after the highlight on purpose. That ordering is
 * what produces the diagonal corners the reference has - highlight wins
 * at (left, bottom-1) while shadow wins at (right, top) - and doing it
 * the other way round is a difference you can measure but never see.
 */
static void raised_box(HDC hdc, const RECT *bx, int pressed)
{
    int l = bx->left, t = bx->top, r = bx->right - 1, bo = bx->bottom - 1;
    COLORREF hi = pressed ? C_SHADOW : C_HILIGHT;
    COLORREF lo = pressed ? C_HILIGHT : C_SHADOW;

    fill(hdc, bx, C_FACE);
    hline(hdc, l, r, t, hi);
    vline(hdc, l, t, bo, hi);
    hline(hdc, l, r, bo, lo);
    vline(hdc, r, t, bo, lo);
    hline(hdc, l + 1, r, bo - 1, lo);
    vline(hdc, r - 1, t + 1, bo, lo);
}

/* The sysmenu box is NOT a button. It has no bevel at all - just the face
   colour - and only the glyph inside is drawn: a 13x3 bar with a black
   outline, a single white row inside it, and a drop shadow offset one
   pixel down and right. Every retro mock-up draws a minus sign here. */
static void sysmenu_box(HDC hdc, const RECT *bx, int barw)
{
    int t = bx->top;
    int a = bx->left + (BTN_W - barw) / 2;   /* centred in the box */
    int b = a + barw - 1;

    fill(hdc, bx, C_FACE);
    /* shadow first, so the outline sits on top of it where they meet */
    vline(hdc, b + 1, t + 8, t + 9, C_SHADOW);
    hline(hdc, a + 1, b + 1, t + 10, C_SHADOW);
    hline(hdc, a, b, t + 7, C_FRAME);
    hline(hdc, a, b, t + 9, C_FRAME);
    vline(hdc, a, t + 7, t + 9, C_FRAME);
    vline(hdc, b, t + 7, t + 9, C_FRAME);
    hline(hdc, a + 1, b - 1, t + 8, C_HILIGHT);
}

/* The frame's bar glyph is 13 px wide; an MDI child's is 7, and so is the
   one a maximised child puts in the menu bar. Measured off both. */
#define BAR_FRAME  13
#define BAR_CHILD  7

/* Four rows, 7-5-3-1 px wide, solid black. Deliberately not Polygon():
   at this size the rasteriser's triangle is not the reference's shape,
   and the difference is a third of the glyph. */
static void arrow(HDC hdc, int apex_x, int top, int up)
{
    int i;

    for (i = 0; i < 4; i++) {
        int w = up ? 1 + i * 2 : 7 - i * 2;

        hline(hdc, apex_x - w / 2, apex_x + w / 2, top + i, C_FRAME);
    }
}

/* Where the three boxes sit. Measured: each arrow button has a 1 px black
   separator on its LEFT and nothing on its right, so the navy runs
   straight up to the minimise button's highlight. */
static void btn_rects(HWND hwnd, RECT *sys, RECT *mn, RECT *mx)
{
    RECT rc;
    int f = FRAME_W();

    GetClientRect(hwnd, &rc);

    sys->left  = f;                 sys->right = f + BTN_W;
    mx->right  = rc.right - f;      mx->left   = mx->right - BTN_W;
    mn->right  = mx->left - 1;      mn->left   = mn->right - BTN_W;

    sys->top = mn->top = mx->top = f;
    sys->bottom = mn->bottom = mx->bottom = f + CAP_H;
}

/* A popup entry. MF_OWNERDRAW replaces the string with a pointer, so the
   label has to live somewhere we own - hence the static tables below.
   id == 0 with no text is a separator. */
typedef struct {
    const char *text;       /* '&' marks the mnemonic */
    const char *accel;      /* right-aligned, or NULL */
    UINT        id;
} MItem;

/* 3.1's control menu, which is what the sysmenu box has always opened.
   Double-clicking the box closes the window; that is 3.1 too. */
static const MItem SYSITEMS[] = {
    { "&Restore",   NULL,     SC_RESTORE  },
    { "&Move",      NULL,     SC_MOVE     },
    { "&Size",      NULL,     SC_SIZE     },
    { "Mi&nimize",  NULL,     SC_MINIMIZE },
    { "Ma&ximize",  NULL,     SC_MAXIMIZE },
    { NULL,         NULL,     0           },
    { "&Close",     "Alt+F4", SC_CLOSE    },
};
#define NSYSITEMS ((int)(sizeof(SYSITEMS) / sizeof(SYSITEMS[0])))


/* Measured off a real 3.11 popup (the client's own Window menu, open, with
   a checkmark and accelerators in it) rather than guessed at, which is
   what these were until now:
     interior starts 1 px inside the black border, and the label starts
     18 px further in - the checkmark sits in that gutter
     an item is 18 rows tall, the same as the caption and the menu bar,
     with its text ink 3 rows down
     a separator is ONE BLACK ROW spanning the full interior width. It is
     not etched; 3.1 has no shadow-and-highlight pair here.
     no drop shadow anywhere - that arrived with Windows 95. */
#define POPUP_GUTTER 18
#define POPUP_ITEM_H 18
#define POPUP_SEP_H  7
#define POPUP_SEP_Y  3      /* the black row within the separator */
#define POPUP_PADR   20     /* space past the accelerator */
static HMENU g_sysmenu;

/* ---- converting the application's own menus to owner-draw ----------
 *
 * chrome_init only borrows the resource's popups; their items are still
 * MF_STRING, so Windows draws them and every measured 3.1 metric above
 * goes unused. MF_OWNERDRAW fixes that, but it REPLACES the item string
 * with a pointer - so the labels have to live somewhere of ours.
 *
 * Somewhere is the global heap, not DGROUP: the 16-bit build has about
 * 10 KB of DGROUP spare and menu text would spend a sixth of it.
 *
 * And the conversion happens on WM_INITMENUPOPUP, not once at startup,
 * because MDI appends the open-window list to the Window menu at runtime.
 * Convert once and those entries are the only Windows-drawn items in an
 * otherwise 3.1 menu, which looks worse than not converting at all. */
#define CONV_MAX   64
#define CONV_TEXT  1536

typedef struct {
    MItem items[CONV_MAX];
    char  text[CONV_TEXT];
} ConvArena;

static HGLOBAL    g_convmem;
static ConvArena FAR *g_conv;
static int        g_convn, g_convt;

/* Reset before a bar menu opens - safe there because nothing is on screen
   yet, and it bounds the arena against MDI's changing window list. */
static void conv_reset(void)
{
    g_convn = 0;
    g_convt = 0;
}

static const MItem FAR *conv_add(const char *label)
{
    MItem FAR *it;
    char FAR *dst;
    const char *src = label;
    int n = lstrlen(label);

    if (!g_conv || g_convn >= CONV_MAX || g_convt + n + 2 > CONV_TEXT)
        return NULL;                /* full: leave the item to Windows */
    it = &g_conv->items[g_convn++];
    dst = g_conv->text + g_convt;
    it->text = dst;
    it->accel = NULL;
    it->id = 0;
    /* An accelerator is the tail after a tab, and it is right-aligned. */
    while (*src && *src != '\t')
        *dst++ = *src++;
    *dst++ = '\0';
    if (*src == '\t') {
        src++;
        it->accel = dst;
        while (*src)
            *dst++ = *src++;
        *dst++ = '\0';
    }
    g_convt = (int)(dst - g_conv->text);
    return it;
}

/* Turn one popup's plain items into owner-draw ones, preserving id, and
   the checked and greyed state. Items already converted are skipped, and
   submenus are left alone - a submenu's own WM_INITMENUPOPUP will do it
   when it opens. */
static void popup_convert(HMENU h)
{
    int i, n;

    if (!g_conv)
        return;
    n = GetMenuItemCount(h);
    for (i = 0; i < n; i++) {
        UINT st = GetMenuState(h, i, MF_BYPOSITION);
        UINT id = GetMenuItemID(h, i);
        const MItem FAR *it;
        char buf[80];

        if (st == (UINT)-1 || (st & (MF_OWNERDRAW | MF_POPUP)))
            continue;
        if (st & MF_SEPARATOR) {
            ModifyMenu(h, i, MF_BYPOSITION | MF_OWNERDRAW | MF_SEPARATOR,
                       0, (LPCSTR)NULL);
            continue;
        }
        if (!GetMenuString(h, i, buf, sizeof(buf) - 1, MF_BYPOSITION))
            continue;
        it = conv_add(buf);
        if (!it)
            continue;
        ModifyMenu(h, i, MF_BYPOSITION | MF_OWNERDRAW
                   | (st & (MF_CHECKED | MF_GRAYED | MF_DISABLED)),
                   id, (LPCSTR)it);
    }
}

/* Build a popup out of a table. MF_OWNERDRAW keeps every behaviour -
   arrow keys, mnemonics, dismissal - and hands us only the pixels. */
static HMENU popup_from(const MItem *items, int n)
{
    HMENU h = CreatePopupMenu();
    int i;

    for (i = 0; i < n; i++)
        AppendMenu(h, MF_OWNERDRAW, items[i].id, (LPCSTR)&items[i]);
    return h;
}

static void popup_measure(MEASUREITEMSTRUCT FAR *mis)
{
    const MItem FAR *it = (const MItem FAR *)mis->itemData;
    HDC hdc = GetDC(NULL);
    HFONT of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    SIZE a, b;

    if (!it || !it->text) {
        mis->itemHeight = POPUP_SEP_H;
        mis->itemWidth = 0;
    } else {
        a.cx = LOWORD(GetTextExtent(hdc, it->text, lstrlen(it->text)));
        b.cx = 0;
        if (it->accel)
            b.cx = LOWORD(GetTextExtent(hdc, it->accel,
                                        lstrlen(it->accel)));
        mis->itemHeight = POPUP_ITEM_H;
        mis->itemWidth = POPUP_GUTTER + a.cx + (b.cx ? b.cx + 24 : 0)
                       + POPUP_PADR;
    }
    SelectObject(hdc, of);
    ReleaseDC(NULL, hdc);
}

static void popup_draw(DRAWITEMSTRUCT FAR *dis)
{
    const MItem FAR *it = (const MItem FAR *)dis->itemData;
    RECT r = dis->rcItem;
    int sel = (dis->itemState & ODS_SELECTED) != 0;
    int dis_ = (dis->itemState & ODS_GRAYED) != 0;
    HFONT of;

    if (!it || !it->text) {
        /* One black row, edge to edge. Measured - the etched shadow and
           highlight this used to draw is a Windows 95 separator. */
        fill(dis->hDC, &r, C_MENU);
        hline(dis->hDC, r.left, r.right - 1, r.top + POPUP_SEP_Y, C_FRAME);
        return;
    }

    fill(dis->hDC, &r, sel ? C_HILITE : C_MENU);
    if (dis->itemState & ODS_CHECKED) {
        /* A tick in the gutter, which is what the gutter is reserved for. */
        int cx = r.left + 5, cy = (r.top + r.bottom) / 2;
        COLORREF c = sel ? C_HILITETX : C_FRAME;

        vline(dis->hDC, cx,     cy,     cy + 2, c);
        vline(dis->hDC, cx + 1, cy + 1, cy + 3, c);
        vline(dis->hDC, cx + 2, cy,     cy + 2, c);
        vline(dis->hDC, cx + 3, cy - 2, cy + 1, c);
        vline(dis->hDC, cx + 4, cy - 4, cy - 1, c);
    }
    SetBkMode(dis->hDC, TRANSPARENT);
    SetTextColor(dis->hDC, dis_ ? C_GRAYTEXT
                                : (sel ? C_HILITETX : C_FRAME));
    of = SelectObject(dis->hDC, GetStockObject(SYSTEM_FONT));
    r.left += POPUP_GUTTER;
    /* No DT_NOPREFIX: the '&' must become the underline. */
    DrawText(dis->hDC, it->text, -1, &r,
             DT_LEFT | DT_VCENTER | DT_SINGLELINE);
    if (it->accel) {
        RECT a = dis->rcItem;

        a.right -= POPUP_PADR;
        DrawText(dis->hDC, it->accel, -1, &a,
                 DT_RIGHT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    }
    SelectObject(dis->hDC, of);
}

/* Open the control menu under the sysmenu box, and grey what does not
   apply - Restore is dead unless maximised, Maximize is dead when it
   already is. 3.1 did exactly this. */
static void sysmenu_drop(HWND hwnd)
{
    RECT sys, mn, mx;
    POINT p;

    btn_rects(hwnd, &sys, &mn, &mx);
    EnableMenuItem(g_sysmenu, SC_RESTORE,
                   MF_BYCOMMAND | (g_maxed ? MF_ENABLED : MF_GRAYED));
    EnableMenuItem(g_sysmenu, SC_MAXIMIZE,
                   MF_BYCOMMAND | (g_maxed ? MF_GRAYED : MF_ENABLED));
    EnableMenuItem(g_sysmenu, SC_SIZE,
                   MF_BYCOMMAND | (g_maxed ? MF_GRAYED : MF_ENABLED));
    p.x = sys.left;
    p.y = sys.bottom + 1;
    ClientToScreen(hwnd, &p);
    TrackPopupMenu(g_sysmenu, TPM_LEFTALIGN | TPM_TOPALIGN | TPM_LEFTBUTTON,
                   p.x, p.y, 0, hwnd, NULL);
}

/* Item rects along the bar, laid end to end from x=1 with MENU_PAD each
   side. Windows would normally compute these; owning the bar means owning
   the arithmetic, and it is the one place a hand-drawn menu can drift out
   of step with where the popup appears. */
/* Where the maximised child's two glyphs go, if there is one. */
static int menu_glyphs(HWND hwnd, RECT *sys, RECT *res)
{
    RECT rc;
    int f = FRAME_W();

    if (!maxchild())
        return 0;
    GetClientRect(hwnd, &rc);
    sys->left   = f;
    sys->right  = f + BTN_W;
    res->right  = rc.right - f;
    res->left   = res->right - BTN_W;
    sys->top = res->top = f + CAP_H + RULE;
    sys->bottom = res->bottom = sys->top + MENU_H;
    return 1;
}

static int menu_layout(HWND hwnd, HDC hdc, RECT out[])
{
    RECT rc, sys, res;
    HFONT of;
    int i, x, f = FRAME_W();

    GetClientRect(hwnd, &rc);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    x = f;
    if (menu_glyphs(hwnd, &sys, &res))
        x = sys.right + 1;      /* past the child's sysmenu box */
    for (i = 0; i < g_nmenus; i++) {
        const char *lbl = g_label[i];
        char plain[32];
        int n = 0;
        SIZE sz;

        /* Measure without the mnemonic marker - '&' is an instruction, not
           a glyph, and counting it makes every item a character too wide. */
        while (*lbl && n < (int)sizeof(plain) - 1) {
            if (*lbl != '&')
                plain[n++] = *lbl;
            lbl++;
        }
        plain[n] = '\0';
        sz.cx = LOWORD(GetTextExtent(hdc, plain, n));

        out[i].left   = x;
        out[i].right  = x + MENU_PAD * 2 + sz.cx;
        out[i].top    = f + CAP_H + RULE;
        out[i].bottom = out[i].top + MENU_H;
        x = out[i].right;
    }
    SelectObject(hdc, of);
    return NMENUS;
}

/* Just the caption strip, or just the menu bar. Repainting the whole
   window to un-press an 18x18 button is a visible flash on a real 3.11
   machine - it was invisible under Wine on a 2026 CPU. */
static void bar_rect(HWND hwnd, RECT *r, int menu)
{
    RECT rc;
    int f = FRAME_W();

    GetClientRect(hwnd, &rc);
    r->left  = f;
    r->right = rc.right - f;
    r->top   = menu ? f + CAP_H + RULE : f;
    r->bottom = menu ? r->top + MENU_H : f + CAP_H;
}

static void inval_bar(HWND hwnd, int menu)
{
    RECT r;

    bar_rect(hwnd, &r, menu);
    InvalidateRect(hwnd, &r, FALSE);
}

static void menu_paint(HWND hwnd, HDC hdc)
{
    RECT rc, bar, items[NMENUS];
    HFONT of;
    int i, f = FRAME_W();

    GetClientRect(hwnd, &rc);
    bar.left = f; bar.right = rc.right - f;
    bar.top = f + CAP_H + RULE;
    bar.bottom = bar.top + MENU_H;
    fill(hdc, &bar, C_MENU);
    hline(hdc, bar.left, bar.right - 1, bar.bottom, C_FRAME);

    /* A maximised child's chrome lives in the bar: its sysmenu box on the
       left and its restore button on the right, with the menu titles
       moved over to make room. This is the one piece of 3.1's MDI
       behaviour that does not come free - MDI inserts those into the menu
       it was handed, and this bar is drawn, not handed over. */
    {
        RECT sys, res;

        if (menu_glyphs(hwnd, &sys, &res)) {
            sysmenu_box(hdc, &sys, BAR_CHILD);
            vline(hdc, sys.right, sys.top, sys.bottom - 1, C_FRAME);
            raised_box(hdc, &res, 0);
            vline(hdc, res.left - 1, res.top, res.bottom - 1, C_FRAME);
            arrow(hdc, res.left + 8, res.top + 4, 1);
            arrow(hdc, res.left + 8, res.top + 10, 0);
        }
    }

    menu_layout(hwnd, hdc, items);
    SetBkMode(hdc, TRANSPARENT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    for (i = 0; i < g_nmenus; i++) {
        RECT t = items[i];
        int open = (i == g_open);

        if (open) {
            /* COLOR_HIGHLIGHT, not black. */
            fill(hdc, &t, C_HILITE);
            SetTextColor(hdc, C_HILITETX);
        } else {
            SetTextColor(hdc, C_FRAME);
        }
        t.left += MENU_PAD;
        t.top  += MENU_TOP;
        /* No DT_NOPREFIX here, on purpose: the '&' has to become the
           underline, which is the only reason it is in the string. */
        DrawText(hdc, g_label[i], -1, &t, DT_LEFT | DT_TOP | DT_SINGLELINE);
    }
    SelectObject(hdc, of);
}

/* Which bar item a client-space point is on, or -1. */
static int menu_at(HWND hwnd, POINT pt)
{
    RECT items[NMENUS];
    HDC hdc = GetDC(hwnd);
    int i, hit = -1;

    menu_layout(hwnd, hdc, items);
    ReleaseDC(hwnd, hdc);
    for (i = 0; i < g_nmenus; i++)
        if (PtInRect(&items[i], pt))
            hit = i;
    return hit;
}

/* Hand the popup to Windows. Everything inside it - arrow keys, mnemonics,
   click-away, submenu timing - is the system's from here, which is the
   whole argument for skinning rather than reimplementing. */
static void menu_drop(HWND hwnd, int i)
{
    RECT items[NMENUS];
    POINT p;
    HDC hdc = GetDC(hwnd);

    menu_layout(hwnd, hdc, items);
    ReleaseDC(hwnd, hdc);
    if (i < 0 || i >= NMENUS || !g_pop[i])
        return;
    conv_reset();
    popup_convert(g_pop[i]);
    g_open = i;
    inval_bar(hwnd, 1);
    UpdateWindow(hwnd);
    p.x = items[i].left;
    p.y = items[i].bottom + 1;
    ClientToScreen(hwnd, &p);
    TrackPopupMenu(g_pop[i], TPM_LEFTALIGN | TPM_TOPALIGN | TPM_LEFTBUTTON,
                   p.x, p.y, 0, hwnd, NULL);
    g_open = -1;
    inval_bar(hwnd, 1);
}

static void cap_paint(HWND hwnd, HDC hdc)
{
    RECT rc, r, sys, mn, mx;
    int active = g_active;
    int f = FRAME_W();
    char title[128];
    int n;
    HFONT of;

    GetClientRect(hwnd, &rc);

    /* The sizing border: a black line outside, a raised bevel inside.
       Skipped when maximised, where 3.1 has no border to grip - which is
       also why the reference cannot verify this part at all. */
    if (f) {
        HBRUSH k = CreateSolidBrush(C_FRAME);
        int a = 1, b = FRAME - 2;           /* the face rows/cols */
        int xl = rc.left + CORNER, xr = rc.right - 1 - CORNER;
        int yt = rc.top + CORNER,  yb = rc.bottom - 1 - CORNER;

        r = rc;
        fill(hdc, &r, C_FACE);
        FrameRect(hdc, &r, k);              /* outer black line */
        InflateRect(&r, -(FRAME - 1), -(FRAME - 1));
        FrameRect(hdc, &r, k);              /* inner black line */
        DeleteObject(k);

        /* Eight corner grips, two per side. */
        vline(hdc, xl, rc.top + a, rc.top + b, C_FRAME);
        vline(hdc, xr, rc.top + a, rc.top + b, C_FRAME);
        vline(hdc, xl, rc.bottom - 1 - b, rc.bottom - 1 - a, C_FRAME);
        vline(hdc, xr, rc.bottom - 1 - b, rc.bottom - 1 - a, C_FRAME);
        hline(hdc, rc.left + a, rc.left + b, yt, C_FRAME);
        hline(hdc, rc.left + a, rc.left + b, yb, C_FRAME);
        hline(hdc, rc.right - 1 - b, rc.right - 1 - a, yt, C_FRAME);
        hline(hdc, rc.right - 1 - b, rc.right - 1 - a, yb, C_FRAME);
    }

    /* Caption: one flat colour, no gradient. */
    r.left = f; r.right = rc.right - f;
    r.top  = f; r.bottom = f + CAP_H;
    fill(hdc, &r, active ? C_ACTIVE : C_INACTIVE);

    hline(hdc, f, rc.right - f - 1, f + CAP_H, C_FRAME);

    btn_rects(hwnd, &sys, &mn, &mx);

    /* Title. Centred in the GAP between the two button clusters, which is
       NOT centred on the caption: the clusters are asymmetric - one box
       left, two right - so the gap's centre sits about 10 px left of the
       window's. Measured, after getting this backwards once. The
       reference's text starts at x=481 on a 1024-wide screen and only the
       gap puts it there. */
    n = GetWindowText(hwnd, title, sizeof(title) - 1);
    title[n < 0 ? 0 : n] = '\0';
    r.left   = sys.right + 1;
    r.right  = mn.left - 1;
    r.top    = f;
    r.bottom = f + CAP_H;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, active ? C_ACTTEXT : C_INACTTEXT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    DrawText(hdc, title, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    SelectObject(hdc, of);

    sysmenu_box(hdc, &sys, BAR_FRAME);
    if (g_down == HIT_SYS) {
        RECT p = sys;

        InflateRect(&p, -1, -1);
        FrameRect(hdc, &p, GetStockObject(GRAY_BRUSH));
    }
    raised_box(hdc, &mn, g_down == HIT_MIN);
    raised_box(hdc, &mx, g_down == HIT_MAX);

    arrow(hdc, mn.left + 8, mn.top + 7, 0);
    if (g_maxed) {
        arrow(hdc, mx.left + 8, mx.top + 4, 1);
        arrow(hdc, mx.left + 8, mx.top + 10, 0);
    } else {
        arrow(hdc, mx.left + 8, mx.top + 7, 1);
    }

    vline(hdc, sys.right, sys.top, sys.bottom - 1, C_FRAME);
    vline(hdc, mn.left - 1, mn.top, mn.bottom - 1, C_FRAME);
    vline(hdc, mx.left - 1, mx.top, mx.bottom - 1, C_FRAME);

    /* Client area, with a note so the screenshot explains itself. */
    menu_paint(hwnd, hdc);

    r.left   = f;
    r.right  = rc.right - f;
    r.top    = f + CAP_H + RULE + MENU_H + 1;
    r.bottom = rc.bottom - f;
    fill(hdc, &r, C_CLIENT);
    SetTextColor(hdc, RGB(0, 0, 0));
    of = SelectObject(hdc, GetStockObject(SYSTEM_FIXED_FONT));
    InflateRect(&r, -8, -8);
    DrawText(hdc,
             "Tier 2 spike.\r\n\r\n"
             "This caption is drawn by the program, not by Windows, and\r\n"
             "every metric in it was measured off a real 3.11 capture\r\n"
             "with a pixel differ rather than remembered.\r\n\r\n"
             "Drag the caption. Drag any edge or corner to resize.\r\n"
             "The buttons work. The corners are square on Windows 11.\r\n\r\n"
             "The menu bar is ours too - white, as 3.1's is, not the grey\r\n"
             "95 changed it to. The popups are real HMENUs handed to\r\n"
             "TrackPopupMenu, so arrow keys and mnemonics still work.",
             -1, &r, DT_LEFT | DT_TOP | DT_NOPREFIX);
    SelectObject(hdc, of);
}

static int cap_button_at(HWND hwnd, POINT pt)
{
    RECT sys, mn, mx;

    btn_rects(hwnd, &sys, &mn, &mx);
    if (PtInRect(&sys, pt)) return HIT_SYS;
    if (PtInRect(&mn, pt))  return HIT_MIN;
    if (PtInRect(&mx, pt))  return HIT_MAX;
    return HIT_NONE;
}

/* The whole reason drag and resize still feel native: we do not implement
   them. We answer "what is under the cursor" with the codes Windows
   already understands, and DefWindowProc runs the move/size loop -
   including edge snapping and the double-click-to-maximise on a caption. */
static LRESULT cap_hittest(HWND hwnd, LPARAM lParam)
{
    RECT rc;
    POINT pt;
    int x, y, l, t, rr, b;

    pt.x = GET_X_LPARAM(lParam);
    pt.y = GET_Y_LPARAM(lParam);
    ScreenToClient(hwnd, &pt);
    GetClientRect(hwnd, &rc);
    x = pt.x; y = pt.y;

    l = (x < FRAME); t = (y < FRAME);
    rr = (x >= rc.right - FRAME); b = (y >= rc.bottom - FRAME);

    if (!g_maxed && (l || t || rr || b)) {
        /* Anywhere in the border within CORNER of a corner resizes
           diagonally - which is exactly what the tick marks delimit, so
           the grip you can see and the grip you can grab agree. A 4 px
           corner (the old behaviour) is very hard to hit on purpose. */
        int nl = (x < CORNER), nr = (x >= rc.right - CORNER);
        int nt = (y < CORNER), nb = (y >= rc.bottom - CORNER);

        if (nl && nt) return HTTOPLEFT;
        if (nr && nt) return HTTOPRIGHT;
        if (nl && nb) return HTBOTTOMLEFT;
        if (nr && nb) return HTBOTTOMRIGHT;
        if (l)  return HTLEFT;
        if (rr) return HTRIGHT;
        if (t)  return HTTOP;
        return HTBOTTOM;
    }
    if (cap_button_at(hwnd, pt) != HIT_NONE)
        return HTCLIENT;            /* ours; we handle the click */
    if (y < FRAME_W() + CAP_H)
        return HTCAPTION;           /* Windows drags it for us */
    return HTCLIENT;                /* menu bar included - never HTCAPTION,
                                       or the bar would drag the window */
}

/* Square the corners off. Windows 11 rounds every top-level window,
   including a WS_POPUP one, and a rounded 1993 window looks like a
   mistake. Loaded at run time so this same binary still runs on Wine and
   on Windows 7, where the call does not exist. */
static void square_corners(HWND hwnd)
{
#ifdef __WATCOMC__
    /* Windows 3.1 has no compositor and no rounded corners to switch off. */
    (void)hwnd;
#else
    HMODULE dwm = LoadLibraryA("dwmapi.dll");
    HRESULT (WINAPI *setattr)(HWND, DWORD, LPCVOID, DWORD);
    DWORD pref = 1;                 /* DWMWCP_DONOTROUND */

    if (!dwm)
        return;
    setattr = (HRESULT (WINAPI *)(HWND, DWORD, LPCVOID, DWORD))
              GetProcAddress(dwm, "DwmSetWindowAttribute");
    if (setattr)
        setattr(hwnd, 33, &pref, sizeof(pref));  /* CORNER_PREFERENCE */
    FreeLibrary(dwm);
#endif
}


/* ---------------------------------------------------------------- API */

/* Tell the chrome which window is the MDI client, so a maximised child's
   glyphs can appear in the menu bar. Call after creating it; NULL if the
   frame has no MDI. */
void chrome_set_mdi(HWND mdiclient)
{
    g_mdi = mdiclient;
}

void chrome_init(HWND hwnd, HMENU bar)
{
    int i, n;

    /* Windows 11 rounds every top-level window, including a WS_POPUP one,
       and a rounded 1993 window looks like a mistake. This was called from
       the spike's WM_CREATE before the extraction; the linker noticing it
       had become unreferenced is the only reason it is not quietly missing
       from the module. */
    square_corners(hwnd);
    g_sysmenu = popup_from(SYSITEMS, NSYSITEMS);
    if (!g_convmem) {
        g_convmem = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT,
                                (DWORD)sizeof(ConvArena));
        if (g_convmem)
            g_conv = (ConvArena FAR *)GlobalLock(g_convmem);
    }
    g_nmenus = 0;
    if (!bar)
        return;
    n = GetMenuItemCount(bar);
    for (i = 0; i < n && g_nmenus < CHROME_MAX_MENUS; i++) {
        HMENU sub = GetSubMenu(bar, i);

        if (!sub)
            continue;       /* a bare command on the bar; 3.1 apps have none */
        GetMenuString(bar, i, g_label[g_nmenus], sizeof(g_label[0]) - 1,
                      MF_BYPOSITION);
        g_pop[g_nmenus] = sub;
        g_nmenus++;
    }
}

int chrome_edge(HWND hwnd)
{
    (void)hwnd;
    return FRAME_W();
}

int chrome_top(HWND hwnd)
{
    (void)hwnd;
    return FRAME_W() + CAP_H + RULE + (g_nmenus ? MENU_H + 1 : 0);
}

void chrome_paint(HWND hwnd, HDC hdc)
{
    cap_paint(hwnd, hdc);
    if (g_nmenus)
        menu_paint(hwnd, hdc);
}

/* ------------------------------------------------ MDI child chrome --- */

/* A child keeps a REAL non-client area, unlike the frame. The frame had to
 * give its up to be rid of the OS menu bar; a child has no menu, so it can
 * simply declare an NC area of the size 3.1 would use and paint it in
 * WM_NCPAINT. That is worth doing deliberately: the child's client rect
 * then IS its content area, so not one line of the application's seven
 * document window procedures has to move.
 *
 * The buttons are not handled here either. WM_NCHITTEST answers HTSYSMENU,
 * HTMINBUTTON and HTMAXBUTTON, and DefMDIChildProc does the rest - which
 * is how the control menu, minimise and maximise-into-frame keep behaving
 * exactly as MDI intends.
 */
static int child_edge(HWND hwnd)
{
    return IsZoomed(hwnd) ? 0 : FRAME;
}

int chrome_child_edge(HWND hwnd)
{
    return child_edge(hwnd);
}

int chrome_child_top(HWND hwnd)
{
    /* Relative to the WINDOW, for anyone laying out by hand. The client
       rect already excludes this. */
    return IsZoomed(hwnd) ? 0 : FRAME + CAP_H + RULE;
}

/* Window-relative button rects. */
static void child_btns(HWND hwnd, RECT *sys, RECT *mn, RECT *mx)
{
    RECT wr;
    int f = child_edge(hwnd), w;

    GetWindowRect(hwnd, &wr);
    w = wr.right - wr.left;
    sys->left  = f;             sys->right = f + BTN_W;
    mx->right  = w - f;         mx->left   = mx->right - BTN_W;
    mn->right  = mx->left - 1;  mn->left   = mn->right - BTN_W;
    sys->top = mn->top = mx->top = f;
    sys->bottom = mn->bottom = mx->bottom = f + CAP_H;
}

void chrome_child_paint(HWND hwnd, HDC hdc, int active)
{
    RECT wr, r, sys, mn, mx;
    int f = child_edge(hwnd), w, h;
    char title[128];
    int n;
    HFONT of;

    if (IsZoomed(hwnd))
        return;                 /* MDI hid the caption; the bar has it */

    GetWindowRect(hwnd, &wr);
    w = wr.right - wr.left;
    h = wr.bottom - wr.top;

    r.left = 0; r.top = 0; r.right = w; r.bottom = h;
    fill(hdc, &r, C_FACE);
    {
        HBRUSH k = CreateSolidBrush(C_FRAME);

        FrameRect(hdc, &r, k);
        InflateRect(&r, -(FRAME - 1), -(FRAME - 1));
        FrameRect(hdc, &r, k);
        DeleteObject(k);
    }

    r.left = f; r.right = w - f;
    r.top = f;  r.bottom = f + CAP_H;
    /* An INACTIVE child caption is white, where the frame's is button
       grey. Measured; reusing the frame's colour looks almost right. */
    fill(hdc, &r, active ? C_ACTIVE : C_MENU);
    hline(hdc, f, w - f - 1, f + CAP_H, C_FRAME);

    child_btns(hwnd, &sys, &mn, &mx);

    n = GetWindowText(hwnd, title, sizeof(title) - 1);
    title[n < 0 ? 0 : n] = '\0';
    r.left = sys.right + 1;
    r.right = mn.left - 1;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, active ? C_ACTTEXT : C_INACTTEXT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    DrawText(hdc, title, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    SelectObject(hdc, of);

    sysmenu_box(hdc, &sys, BAR_CHILD);
    raised_box(hdc, &mn, g_cdown_wnd == hwnd && g_cdown_hit == HTMINBUTTON);
    raised_box(hdc, &mx, g_cdown_wnd == hwnd && g_cdown_hit == HTMAXBUTTON);
    arrow(hdc, mn.left + 8, mn.top + 7, 0);
    arrow(hdc, mx.left + 8, mx.top + 7, 1);
    vline(hdc, sys.right, sys.top, sys.bottom - 1, C_FRAME);
    vline(hdc, mn.left - 1, mn.top, mn.bottom - 1, C_FRAME);
    vline(hdc, mx.left - 1, mx.top, mx.bottom - 1, C_FRAME);
}

/* Is this the child MDI considers active? Asked here rather than passed
   in, so the application does not have to know. */
static int child_is_active(HWND hwnd)
{
    HWND p = GetParent(hwnd);

    if (!p)
        return 0;
    return LLM_HWND(SendMessage(p, WM_MDIGETACTIVE, 0, 0L)) == hwnd;
}

/* Paint the child's non-client area - and ONLY that.
 *
 * A window DC covers the whole window, client area included, so filling
 * the window rect here scribbles over everything the application drew:
 * the transcript, the picture, the music panel all go flat grey and stay
 * that way until something happens to invalidate them. Excluding the
 * client rect first is what makes this non-client painting rather than
 * vandalism. The frame does not need it because the frame HAS no
 * non-client area - its client rect is the whole window, and filling it
 * is the intent.
 */
static void child_nc_active(HWND hwnd, int active)
{
    HDC hdc = GetWindowDC(hwnd);
    RECT wr, cr;
    POINT o;

    if (!hdc)
        return;
    GetWindowRect(hwnd, &wr);
    GetClientRect(hwnd, &cr);
    o.x = 0; o.y = 0;
    ClientToScreen(hwnd, &o);
    ExcludeClipRect(hdc, o.x - wr.left, o.y - wr.top,
                    o.x - wr.left + cr.right, o.y - wr.top + cr.bottom);
    chrome_child_paint(hwnd, hdc, active);
    ReleaseDC(hwnd, hdc);
}

static void child_nc(HWND hwnd)
{
    child_nc_active(hwnd, child_is_active(hwnd));
}

int chrome_child_msg(HWND hwnd, UINT msg, UINT wParam, LONG lParam,
                     LONG *result)
{
    POINT pt;
    RECT wr, sys, mn, mx;
    int f, x, y, w, h;

    *result = 0;
    switch (msg) {
    /* Declare the non-client area 3.1 would use. Both forms: the FALSE
       one is a bare RECT, the TRUE one an NCCALCSIZE_PARAMS whose first
       rect is the proposed client area. */
    case WM_NCCALCSIZE: {
        RECT FAR *r = wParam
                    ? &((NCCALCSIZE_PARAMS FAR *)lParam)->rgrc[0]
                    : (RECT FAR *)lParam;

        f = child_edge(hwnd);
        r->left   += f;
        r->right  -= f;
        r->bottom -= f;
        r->top    += chrome_child_top(hwnd);
        if (wParam)
            *result = WVR_REDRAW;   /* or a move copies stale bits */
        return 1;
    }

    case WM_NCPAINT:
        child_nc(hwnd);
        return 1;

    /* Setting the title is the other route to a default caption being
       painted - the picture window renames itself as images arrive. Let
       the text change happen, then repaint over whatever drew itself. */
    /* Do the button ourselves. Answering HTMINBUTTON/HTMAXBUTTON tells
       DefMDIChildProc where the buttons are, and it then tracks the press
       by drawing ITS buttons at ITS idea of the position - which is the
       host-styled pair that appears alongside ours, depresses, and does
       nothing, because the window it is drawing on is not the one it
       thinks. Claiming the click stops the tracking before it starts. */
    case WM_NCLBUTTONDOWN:
        if (wParam == HTMINBUTTON || wParam == HTMAXBUTTON) {
            g_cdown_wnd = hwnd;
            g_cdown_hit = (int)wParam;
            SetCapture(hwnd);
            child_nc(hwnd);         /* show it depressed */
            return 1;
        }
        return 0;                   /* HTCAPTION and the edges are MDI's */

    case WM_LBUTTONUP:
        if (g_cdown_wnd == hwnd) {
            int was = g_cdown_hit;
            POINT sp;

            g_cdown_wnd = NULL;
            g_cdown_hit = 0;
            ReleaseCapture();
            child_nc(hwnd);         /* let it back up */
            /* Only act if the pointer is still on the button it went down
               on - the release comes here through the capture wherever it
               happens. */
            sp.x = GET_X_LPARAM(lParam);
            sp.y = GET_Y_LPARAM(lParam);
            ClientToScreen(hwnd, &sp);
            if (SendMessage(hwnd, WM_NCHITTEST, 0,
                            MAKELONG(sp.x, sp.y)) == was) {
                if (was == HTMINBUTTON)
                    ShowWindow(hwnd, SW_MINIMIZE);
                else
                    /* WM_MDIMAXIMIZE, not ShowWindow: MDI has to know, or
                       the frame caption never picks up " - [Title]". */
                    SendMessage(GetParent(hwnd), WM_MDIMAXIMIZE,
                                (UINT)hwnd, 0L);
            }
            return 1;
        }
        return 0;

    case WM_SETTEXT:
        PostMessage(hwnd, WM_NCPAINT, 1, 0L);
        return 0;

    /* Maximising or restoring a child changes the FRAME: the child's
       sysmenu box and restore button appear in or vanish from the menu
       bar, and the menu titles shift to make room. Nothing tells the
       frame that, so without this the bar keeps whatever it had - which
       looks like a maximise glyph floating on its own over stale pixels
       until something else happens to invalidate the window. */
    case WM_SIZE:
        if (wParam == SIZE_MAXIMIZED || wParam == SIZE_RESTORED) {
            HWND mdi = GetParent(hwnd);
            HWND frame = mdi ? GetParent(mdi) : NULL;

            if (frame)
                InvalidateRect(frame, NULL, TRUE);
        }
        return 0;

    /* Paint ours and CLAIM it. Returning 0 here lets DefMDIChildProc
       paint the standard caption straight afterwards, and wherever ours
       does not cover it the OS one shows through - which on Windows 11 is
       Aero peeking out around the edges every time the window is clicked
       away from and back to. TRUE means "activation handled, do not
       draw"; the MDI bookkeeping is WM_MDIACTIVATE's job, not this
       message's. */
    case WM_NCACTIVATE:
        child_nc(hwnd);
        *result = TRUE;
        return 1;

    /* Take the answer from the message, do not go asking for it.
       WM_MDIGETACTIVE still names the OLD child while this is being
       delivered, so a child losing activation would repaint itself
       active - and every window ends up with a navy caption at once. */
    case WM_MDIACTIVATE:
        child_nc_active(hwnd, LLM_MDI_ACTIVE(wParam, lParam, hwnd));
        /* DefMDIChildProc has real work to do on this one, so it has to
           see it - and it may repaint the frame while doing so. Queue our
           own repaint behind it rather than trying to paint first. */
        PostMessage(hwnd, WM_NCPAINT, 1, 0L);
        return 0;

    /* Clicking a child's CONTENT activates it through WM_CHILDACTIVATE,
       not WM_MDIACTIVATE - and only the winner is told, so the window
       losing the caption never repaints and two of them look active at
       once. Repaint every sibling instead. Sent as WM_NCPAINT rather than
       painted directly, so a sibling that is not one of ours still does
       whatever is right for it. */
    case WM_CHILDACTIVATE: {
        HWND c = GetWindow(GetParent(hwnd), GW_CHILD);

        while (c) {
            SendMessage(c, WM_NCPAINT, 1, 0L);
            c = GetWindow(c, GW_HWNDNEXT);
        }
        return 0;
    }

    /* Answer with the codes DefMDIChildProc already knows, and it runs
       the control menu, the minimise and the maximise itself. */
    case WM_NCHITTEST:
        f = child_edge(hwnd);
        GetWindowRect(hwnd, &wr);
        x = GET_X_LPARAM(lParam) - wr.left;
        y = GET_Y_LPARAM(lParam) - wr.top;
        w = wr.right - wr.left;
        h = wr.bottom - wr.top;
        if (!f)
            return 0;               /* maximised: all MDI's */
        if (x < f && y < f)              { *result = HTTOPLEFT;     return 1; }
        if (x >= w - f && y < f)         { *result = HTTOPRIGHT;    return 1; }
        if (x < f && y >= h - f)         { *result = HTBOTTOMLEFT;  return 1; }
        if (x >= w - f && y >= h - f)    { *result = HTBOTTOMRIGHT; return 1; }
        if (x < f)      { *result = HTLEFT;   return 1; }
        if (x >= w - f) { *result = HTRIGHT;  return 1; }
        if (y < f)      { *result = HTTOP;    return 1; }
        if (y >= h - f) { *result = HTBOTTOM; return 1; }
        child_btns(hwnd, &sys, &mn, &mx);
        pt.x = x; pt.y = y;
        if (PtInRect(&sys, pt)) { *result = HTSYSMENU;   return 1; }
        if (PtInRect(&mn, pt))  { *result = HTMINBUTTON; return 1; }
        if (PtInRect(&mx, pt))  { *result = HTMAXBUTTON; return 1; }
        if (y < f + CAP_H)      { *result = HTCAPTION;   return 1; }
        return 0;
    }
    return 0;
}

/* ---------------------------------------------------- dialog chrome --- */

/* Dialogs take the opposite approach to everything else in this file, on
 * purpose.
 *
 * The frame and the children DECLARE their non-client area and the layout
 * is adjusted to suit. A dialog cannot: its controls come from a resource
 * template at fixed client-relative positions, laid out against whatever
 * client rect the host produced. Move that rect and the controls move
 * with it, and the top row ends up under the caption. Two attempts to
 * compute the difference and resize the window to compensate both got it
 * wrong - the second used AdjustWindowRect, which is the right API, and
 * it was still wrong on 3.11.
 *
 * So: do not touch WM_NCCALCSIZE. Let the host reserve exactly what it
 * always would, leave the client rect alone so no control can move, and
 * paint OUR caption into whatever band the host set aside - measured from
 * the window rect against the client origin rather than assumed. The
 * caption is then the host's height, which on 3.11 is within a pixel or
 * two of 3.1's and on Windows 11 is taller than period. That is a real
 * fidelity cost, accepted knowingly: a dialog whose buttons are where the
 * designer put them beats one that is two pixels more authentic and
 * unusable.
 */

/* The non-client band, in window coordinates: bx is the border width, by
   the whole top strip (border plus caption). */
static void dlg_nc_metrics(HWND dlg, RECT *wr, int *bx, int *by)
{
    RECT cr;
    POINT o;

    GetWindowRect(dlg, wr);
    GetClientRect(dlg, &cr);
    o.x = 0; o.y = 0;
    ClientToScreen(dlg, &o);
    *bx = (int)(o.x - wr->left);
    *by = (int)(o.y - wr->top);
    if (*bx < 1) *bx = 1;
    if (*by < *bx + 8) *by = *bx + 8;
}

static void dlg_sysbox(HWND dlg, RECT *sys)
{
    RECT wr;
    int bx, by;

    dlg_nc_metrics(dlg, &wr, &bx, &by);
    sys->left   = bx;
    sys->right  = bx + BTN_W;
    sys->top    = bx;
    sys->bottom = by - 1;
}

void chrome_dialog_paint(HWND dlg, HDC hdc, int active)
{
    RECT wr, r, sys;
    int w, h, bx, by, n;
    char title[128];
    HFONT of;

    dlg_nc_metrics(dlg, &wr, &bx, &by);
    w = wr.right - wr.left;
    h = wr.bottom - wr.top;

    r.left = 0; r.top = 0; r.right = w; r.bottom = h;
    fill(hdc, &r, C_FACE);
    {
        HBRUSH k = CreateSolidBrush(C_FRAME);

        FrameRect(hdc, &r, k);
        if (bx >= 3) {
            InflateRect(&r, -(bx - 1), -(bx - 1));
            FrameRect(hdc, &r, k);
        }
        DeleteObject(k);
    }

    r.left = bx; r.right = w - bx;
    r.top = bx;  r.bottom = by - 1;
    fill(hdc, &r, active ? C_ACTIVE : C_INACTIVE);
    hline(hdc, bx, w - bx - 1, by - 1, C_FRAME);

    dlg_sysbox(dlg, &sys);
    n = GetWindowText(dlg, title, sizeof(title) - 1);
    title[n < 0 ? 0 : n] = '\0';
    r.left = sys.right + 1;
    r.right = w - bx;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, active ? C_ACTTEXT : C_INACTTEXT);
    of = SelectObject(hdc, GetStockObject(SYSTEM_FONT));
    DrawText(hdc, title, -1, &r,
             DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
    SelectObject(hdc, of);

    sysmenu_box(hdc, &sys, BAR_FRAME);
    vline(hdc, sys.right, sys.top, sys.bottom - 1, C_FRAME);
}

static void dlg_nc(HWND dlg, int active)
{
    HDC hdc = GetWindowDC(dlg);
    RECT wr, cr;
    POINT o;

    if (!hdc)
        return;
    GetWindowRect(dlg, &wr);
    GetClientRect(dlg, &cr);
    o.x = 0; o.y = 0;
    ClientToScreen(dlg, &o);
    /* Exclude the client area, or this paints over every control the
       template put there. */
    ExcludeClipRect(hdc, o.x - wr.left, o.y - wr.top,
                    o.x - wr.left + cr.right, o.y - wr.top + cr.bottom);
    chrome_dialog_paint(dlg, hdc, active);
    ReleaseDC(dlg, hdc);
}

int chrome_dialog_msg(HWND dlg, UINT msg, UINT wParam, LONG lParam,
                      LONG *result)
{
    RECT wr, sys;
    int x, y, bx, by;
    POINT pt;

    *result = 0;
    switch (msg) {
    /* Deliberately NOT WM_NCCALCSIZE - see the note above. */
    case WM_NCPAINT:
        dlg_nc(dlg, GetActiveWindow() == dlg);
        return 1;

    case WM_NCACTIVATE:
        dlg_nc(dlg, wParam != 0);
        *result = TRUE;
        return 1;

    case WM_SETTEXT:
        PostMessage(dlg, WM_NCPAINT, 1, 0L);
        return 0;

    case WM_NCHITTEST:
        dlg_nc_metrics(dlg, &wr, &bx, &by);
        x = GET_X_LPARAM(lParam) - wr.left;
        y = GET_Y_LPARAM(lParam) - wr.top;
        dlg_sysbox(dlg, &sys);
        pt.x = x; pt.y = y;
        if (PtInRect(&sys, pt)) { *result = HTSYSMENU; return 1; }
        if (y < by)             { *result = HTCAPTION; return 1; }
        return 0;

    /* Double-clicking the control box closes the window - 3.1, and every
       Windows since. A dialog closes by its cancel command, which is what
       its Close or Cancel button sends. */
    case WM_NCLBUTTONDBLCLK:
        if (wParam == HTSYSMENU) {
            PostMessage(dlg, WM_COMMAND, IDCANCEL, 0L);
            return 1;
        }
        return 0;

    /* Every dialog's buttons, skinned, with nothing to remember at the
       call site: the four dialog procs already route WM_INITDIALOG
       through here, and returning 0 lets their own handler run after. */
    case WM_INITDIALOG:
        chrome_buttons(dlg);
        return 0;

    case WM_ERASEBKGND:
        {
            RECT rc;

            GetClientRect(dlg, &rc);
            fill((HDC)wParam, &rc, C_FACE);
        }
        *result = 1;
        return 1;
    }
    /* WM_CTLCOLOR is deliberately NOT handled here - see chrome_ctlcolor
       and the note in chrome.h. Its answer is a brush, and a brush
       cannot come back through DWL_MSGRESULT. */
    return 0;
}

/* ---- controls -------------------------------------------------------
 *
 * The last surfaces the host still draws for us. A push button on
 * Windows 11 is a 1995 button in 2026 grey - square corners, a one pixel
 * bevel, and a #F0F0F0 face on our #C0C0C0 strip. A real 3.1 button is
 * rounder and deeper than that, and the differences are all measurable:
 *
 *   - the four corner pixels are NOT drawn. The parent shows through,
 *     which is what makes a 3.1 button look rounded.
 *   - the bevel is TWO pixels, not one, on all four sides.
 *   - highlight and shadow are mitred at 45 degrees where they meet, so
 *     the top-right and bottom-left corners are diagonal.
 *
 * Measured off Play/Pause/Stop/Next in the Music window of the reference
 * capture - the same picture the caption came from, so the same
 * methodology and the same standard of proof.
 */

/* One pixel, spelled as the degenerate line the other primitives are. */
static void dot(HDC hdc, int x, int y, COLORREF c)
{
    vline(hdc, x, y, y, c);
}

/* The four corner pixels, which is what rounds a 3.1 button off. White
   on all four - bottom right included, where a bevel would put shadow.
   Measured; guessing gave face grey, and those four pixels were the only
   thing between this and the reference. */
static void round_off(HDC hdc, const RECT *r)
{
    dot(hdc, r->left, r->top, C_HILIGHT);
    dot(hdc, r->right - 1, r->top, C_HILIGHT);
    dot(hdc, r->left, r->bottom - 1, C_HILIGHT);
    dot(hdc, r->right - 1, r->bottom - 1, C_HILIGHT);
}

/* Drawing order is load-bearing. Highlight first and shadow second is
   what produces the mitre: on the highlight's top row the shadow's own
   right column overwrites the last pixel, and on the shadow's bottom row
   the highlight's left column survives one pixel in. Swap the two loops
   and the corners square off. */
void chrome_button_face(HDC hdc, const RECT *bx, int pressed, int deflt)
{
    RECT r = *bx, in;
    int l, t, ri, b, i;

    /* A default button is the same button one pixel smaller inside a
       black ring - which is why 3.1's OK button looks heavier rather
       than merely outlined. */
    if (deflt) {
        l = r.left; t = r.top; ri = r.right - 1; b = r.bottom - 1;
        hline(hdc, l + 1, ri - 1, t, C_FRAME);
        hline(hdc, l + 1, ri - 1, b, C_FRAME);
        vline(hdc, l, t + 1, b - 1, C_FRAME);
        vline(hdc, ri, t + 1, b - 1, C_FRAME);
        round_off(hdc, &r);
        InflateRect(&r, -1, -1);
    }

    l = r.left; t = r.top; ri = r.right - 1; b = r.bottom - 1;
    if (ri - l < 5 || b - t < 5)
        return;

    in = r;
    InflateRect(&in, -1, -1);
    fill(hdc, &in, C_FACE);

    hline(hdc, l + 1, ri - 1, t, C_FRAME);
    hline(hdc, l + 1, ri - 1, b, C_FRAME);
    vline(hdc, l, t + 1, b - 1, C_FRAME);
    vline(hdc, ri, t + 1, b - 1, C_FRAME);

    for (i = 0; i < 2; i++) {
        COLORREF c = pressed ? C_SHADOW : C_HILIGHT;

        hline(hdc, l + 1 + i, ri - 1 - i, t + 1 + i, c);
        vline(hdc, l + 1 + i, t + 1 + i, b - 1 - i, c);
    }
    if (!pressed)
        for (i = 0; i < 2; i++) {
            hline(hdc, l + 1 + i, ri - 1 - i, b - 1 - i, C_SHADOW);
            vline(hdc, ri - 1 - i, t + 1 + i, b - 1 - i, C_SHADOW);
        }
    round_off(hdc, &r);
}

/* 3.1's checkbox is flat: a 13x13 black box on white with a black X
   drawn corner to corner one pixel inside it. No bevel, no sunken well,
   and emphatically no tick - the checkmark arrived with Windows 95, and
   it is the single loudest wrong detail in a retro mock-up after the
   caption. Measured off "Illustrate every room". */
#define CHECK_BOX   13

void chrome_checkbox_face(HDC hdc, int x, int y, int checked, int off)
{
    RECT box;
    int i;

    box.left = x; box.top = y;
    box.right = x + CHECK_BOX; box.bottom = y + CHECK_BOX;
    fill(hdc, &box, C_CLIENT);
    hline(hdc, x, x + CHECK_BOX - 1, y, C_FRAME);
    hline(hdc, x, x + CHECK_BOX - 1, y + CHECK_BOX - 1, C_FRAME);
    vline(hdc, x, y, y + CHECK_BOX - 1, C_FRAME);
    vline(hdc, x + CHECK_BOX - 1, y, y + CHECK_BOX - 1, C_FRAME);
    if (!checked)
        return;
    for (i = 0; i < CHECK_BOX - 2; i++) {
        COLORREF c = off ? C_SHADOW : C_FRAME;

        vline(hdc, x + 1 + i, y + 1 + i, y + 1 + i, c);
        vline(hdc, x + CHECK_BOX - 2 - i, y + 1 + i, y + 1 + i, c);
    }
}

/* The label. Disabled text in 3.1 is embossed rather than merely pale:
   the string is drawn twice, white one pixel down and right, then grey
   on top of it. */
static void btn_text(HDC hdc, HWND hwnd, RECT *tr, UINT fmt, int off)
{
    char text[80];
    int n = GetWindowText(hwnd, text, sizeof(text) - 1);
    HFONT f = (HFONT)SendMessage(hwnd, WM_GETFONT, 0, 0L);
    HFONT of;

    text[n < 0 ? 0 : n] = '\0';
    of = SelectObject(hdc, f ? f : GetStockObject(SYSTEM_FONT));
    SetBkMode(hdc, TRANSPARENT);
    if (off) {
        RECT sr = *tr;

        OffsetRect(&sr, 1, 1);
        SetTextColor(hdc, C_HILIGHT);
        DrawText(hdc, text, -1, &sr, fmt);
        SetTextColor(hdc, C_SHADOW);
    } else {
        SetTextColor(hdc, C_FRAME);
    }
    DrawText(hdc, text, -1, tr, fmt);
    SelectObject(hdc, of);
}

static void btn_paint(HWND hwnd, HDC hdc)
{
    RECT rc, tr;
    LONG style = GetWindowLong(hwnd, GWL_STYLE);
    int kind = (int)(style & 0x0FL);
    UINT st = (UINT)SendMessage(hwnd, BM_GETSTATE, 0, 0L);
    int pressed = (st & BST_PUSHED) != 0;
    int off = !IsWindowEnabled(hwnd);

    GetClientRect(hwnd, &rc);
    /* Face grey unconditionally, rather than asking the parent what
       colour it is. Every button in this program sits on an LTGRAY_BRUSH
       strip or on a dialog, and both of those are #C0C0C0 now; asking
       WM_CTLCOLOR would route through DefWindowProc on the way and get
       2026's answer back. */
    fill(hdc, &rc, C_FACE);

    if (kind == BS_CHECKBOX || kind == BS_AUTOCHECKBOX
        || kind == BS_3STATE || kind == BS_AUTO3STATE) {
        int y = rc.top + (rc.bottom - rc.top - CHECK_BOX) / 2;

        chrome_checkbox_face(hdc, rc.left, y,
                    (int)SendMessage(hwnd, BM_GETCHECK, 0, 0L) != 0, off);
        tr = rc;
        /* Measured: the box is 13 wide and the label's ink starts 6 px
           past its right edge. */
        tr.left += CHECK_BOX + 6;
        btn_text(hdc, hwnd, &tr, DT_LEFT | DT_VCENTER | DT_SINGLELINE, off);
        if (st & BST_FOCUS) {
            RECT fr = rc;

            fr.left += CHECK_BOX + 4;
            InflateRect(&fr, 0, -2);
            DrawFocusRect(hdc, &fr);
        }
        return;
    }

    chrome_button_face(hdc, &rc, pressed,
                       kind == BS_DEFPUSHBUTTON);
    tr = rc;
    if (pressed)                    /* the label rides the bevel down */
        OffsetRect(&tr, 1, 1);
    btn_text(hdc, hwnd, &tr, DT_CENTER | DT_VCENTER | DT_SINGLELINE, off);
    if (st & BST_FOCUS) {
        RECT fr = rc;

        InflateRect(&fr, -4, -4);
        DrawFocusRect(hdc, &fr);
    }
}

/* Painting a BUTTON rather than owner-drawing it. BS_OWNERDRAW is the
   obvious route and it is a trap: the dialog manager moves the default
   ring from button to button with BM_SETSTYLE as the focus travels, and
   BS_OWNERDRAW lives in the same low nibble as BS_DEFPUSHBUTTON - so the
   first Tab turns an owner-drawn button back into an ordinary one and it
   never draws again. Subclassing touches no style bit. The class goes on
   tracking pressed state, focus, mnemonics, the space bar and the click;
   all we take from it is WM_PAINT.

   One saved proc for all of them, because every BUTTON in the process
   shares one class procedure - there is nothing per-window to remember,
   and so nothing to leak when a window dies. */
static LlmOldProc g_btnproc;

LONG FAR PASCAL _export chrome_btnproc(HWND hwnd, UINT msg, UINT wParam,
                                       LONG lParam)
{
    PAINTSTRUCT ps;

    switch (msg) {
    case WM_ERASEBKGND:
        return 1;               /* WM_PAINT covers every pixel we own */

    case WM_PAINT:
        BeginPaint(hwnd, &ps);
        btn_paint(hwnd, ps.hdc);
        EndPaint(hwnd, &ps);
        return 0;
    }
    return CallWindowProc(g_btnproc, hwnd, msg, wParam, lParam);
}

void chrome_button(HWND btn)
{
    LONG old;

    if (!btn)
        return;
    old = SetWindowLong(btn, GWL_WNDPROC, (LONG)(LlmOldProc)chrome_btnproc);
    if ((LlmOldProc)old == (LlmOldProc)chrome_btnproc)
        return;                 /* already ours - put nothing in g_btnproc */
    if (!g_btnproc)
        g_btnproc = (LlmOldProc)old;
}

void chrome_buttons(HWND parent)
{
    HWND c;

    for (c = GetWindow(parent, GW_CHILD); c;
         c = GetWindow(c, GW_HWNDNEXT)) {
        char cls[16];

        if (GetClassName(c, cls, sizeof(cls)) > 0
            && lstrcmpi(cls, "button") == 0)
            chrome_button(c);
    }
}

/* The 3.1 palette for whatever the host would otherwise colour itself:
   static text and buttons on face grey, edits and lists on white, all of
   it with black text. Without this a dialog on Windows 11 is #F0F0F0
   with #C0C0C0 chrome around it, which reads as a rendering fault rather
   than as a style. */
int chrome_ctlcolor(UINT msg, UINT wParam, LONG lParam, LONG *result)
{
    HDC hdc = LLM_CTLCOLOR_DC(wParam);
    int kind = LLM_CTLCOLOR_KIND(msg, lParam);
    static HBRUSH face, paper;

    (void)msg;      /* 3.1 has one message and says the kind in lParam */
    (void)lParam;

    switch (kind) {
    case CTLCOLOR_EDIT:
    case CTLCOLOR_LISTBOX:
        if (!paper)
            paper = CreateSolidBrush(C_CLIENT);
        SetBkColor(hdc, C_CLIENT);
        SetTextColor(hdc, C_FRAME);
        *result = (LONG)paper;
        return 1;
    case CTLCOLOR_BTN:
    case CTLCOLOR_STATIC:
    case CTLCOLOR_DLG:
    case CTLCOLOR_MSGBOX:
        if (!face)
            face = CreateSolidBrush(C_FACE);
        SetBkColor(hdc, C_FACE);
        SetTextColor(hdc, C_FRAME);
        *result = (LONG)face;
        return 1;
    }
    return 0;
}

int chrome_msg(HWND hwnd, UINT msg, UINT wParam, LONG lParam, LONG *result)
{
    POINT pt;

    *result = 0;
    switch (msg) {
    case WM_NCCALCSIZE:
        /* Both forms, not just wParam=TRUE. Letting DefWindowProc answer
           the FALSE one insets the rect by a frame we do not draw, the two
           answers disagree by the frame width, and the strip between them
           belongs to nobody and never repaints.

           WVR_REDRAW on the TRUE form, not 0: 0 means "preserve the client
           area", so Windows copies the old bits on a move and whatever it
           does not copy is left stale - which is what notches the border
           corners. WVR_REDRAW says redraw the lot. The rect we leave
           untouched still defines the client area; this only changes the
           preserve policy. */
        if (wParam)
            *result = WVR_REDRAW;
        return 1;

    case WM_NCHITTEST:
        *result = cap_hittest(hwnd, lParam);
        return 1;

    /* There is no non-client area to paint, but DefWindowProc does not
       know that during a resize and briefly draws a default frame - which
       shows up on Windows 11 as old chrome flashing between frames. */
    case WM_NCPAINT:
        *result = 0;
        return 1;

    /* See the child's copy of this: a partial update region after a move
       leaves the border corners unpainted. */
    case WM_WINDOWPOSCHANGED:
        InvalidateRect(hwnd, NULL, TRUE);
        return 0;

    /* We cover every pixel in WM_PAINT, so letting anything erase first
       only produces a flash. */
    case WM_ERASEBKGND:
        *result = 1;
        return 1;

    /* The control menu's items ARE system commands, and something has to
       turn the WM_COMMAND they generate into WM_SYSCOMMAND. The spike did
       that before the chrome was extracted and nothing did it afterwards,
       so Close stopped closing and Move and Size stopped working - the
       menu opened and every item was inert. The 0xF000 test is the
       documented range for system commands; an application's own menu ids
       are far below it. */
    case WM_COMMAND:
        if ((wParam & 0xFFF0) >= 0xF000) {
            PostMessage(hwnd, WM_SYSCOMMAND, wParam & 0xFFF0, 0L);
            return 1;
        }
        return 0;

    /* Alt+Space opens the control menu, and Alt+letter drops the matching
       bar menu. Windows asks for both through SC_KEYMENU, and normally
       DefWindowProc answers using the window's real system menu and real
       menu bar. We have neither - both are ours - so both arrive here and
       do nothing unless we answer them. */
    case WM_SYSCOMMAND:
        if ((wParam & 0xFFF0) == SC_KEYMENU) {
            int i;
            char c = (char)lParam;

            if (c == ' ') {
                sysmenu_drop(hwnd);
                return 1;
            }
            for (i = 0; i < g_nmenus; i++) {
                const char *l = g_label[i];

                while (*l && *l != '&')
                    l++;
                if (*l == '&' && l[1]
                    && (char)(l[1] | 0x20) == (char)(c | 0x20)) {
                    menu_drop(hwnd, i);
                    return 1;
                }
            }
        }
        return 0;

    case WM_GETMINMAXINFO: {
        MINMAXINFO FAR *mmi = (MINMAXINFO FAR *)lParam;
        RECT wa;

        LLM_WORKAREA(&wa);
        mmi->ptMaxPosition.x = wa.left;
        mmi->ptMaxPosition.y = wa.top;
        mmi->ptMaxSize.x = wa.right - wa.left;
        mmi->ptMaxSize.y = wa.bottom - wa.top;
        mmi->ptMaxTrackSize = mmi->ptMaxSize;
        return 1;
    }

    case WM_NCACTIVATE:
        /* Repaint for the caption colour, and return TRUE so Windows does
           not try to draw a caption we do not have. */
        g_active = (wParam != 0);
        inval_bar(hwnd, 0);
        *result = TRUE;
        return 1;

    case WM_ACTIVATE:
        inval_bar(hwnd, 0);
        return 0;               /* the app may want this too */

    case WM_SIZE:
        g_maxed = (wParam == SIZE_MAXIMIZED);
        return 0;               /* the app lays its children out on this */

    /* Submenus, and anything the application rebuilt since it last
       opened - MDI's window list above all. */
    case WM_INITMENUPOPUP:
        popup_convert((HMENU)wParam);
        return 0;

    /* ODT_MENU only. The application owner-draws its own controls too -
       LLM64's launcher strip is a row of owner-drawn buttons - and
       swallowing every WM_DRAWITEM would blank them. */
    case WM_MEASUREITEM:
        if (((MEASUREITEMSTRUCT FAR *)lParam)->CtlType != ODT_MENU)
            return 0;
        popup_measure((MEASUREITEMSTRUCT FAR *)lParam);
        *result = TRUE;
        return 1;

    case WM_DRAWITEM:
        if (((DRAWITEMSTRUCT FAR *)lParam)->CtlType != ODT_MENU)
            return 0;
        popup_draw((DRAWITEMSTRUCT FAR *)lParam);
        *result = TRUE;
        return 1;

    case WM_LBUTTONDOWN:
        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        g_bar_down = g_nmenus ? menu_at(hwnd, pt) : -1;
        if (g_bar_down >= 0) {
            g_open = g_bar_down;            /* highlight on the press */
            inval_bar(hwnd, 1);
            return 1;
        }
        {
            RECT sys, res;
            HWND mc = maxchild();

            if (mc && menu_glyphs(hwnd, &sys, &res)) {
                if (PtInRect(&res, pt)) {
                    SendMessage(g_mdi, WM_MDIRESTORE, (UINT)mc, 0L);
                    return 1;
                }
                if (PtInRect(&sys, pt)) {
                    SendMessage(mc, WM_SYSCOMMAND, SC_KEYMENU, (LONG)' ');
                    return 1;
                }
            }
        }
        g_down = cap_button_at(hwnd, pt);
        if (g_down != HIT_NONE) {
            SetCapture(hwnd);
            inval_bar(hwnd, 0);
            return 1;
        }
        return 0;                           /* not ours - the app's click */

    case WM_LBUTTONUP:
        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        if (g_bar_down >= 0) {
            int m = g_bar_down;

            g_bar_down = -1;
            if (menu_at(hwnd, pt) == m) {
                menu_drop(hwnd, m);
            } else {
                g_open = -1;
                inval_bar(hwnd, 1);
            }
            return 1;
        }
        if (g_down != HIT_NONE) {
            int was = g_down;

            g_down = HIT_NONE;
            ReleaseCapture();
            inval_bar(hwnd, 0);
            if (cap_button_at(hwnd, pt) == was) {
                if (was == HIT_MIN)
                    ShowWindow(hwnd, SW_MINIMIZE);
                else if (was == HIT_MAX)
                    ShowWindow(hwnd, g_maxed ? SW_RESTORE : SW_MAXIMIZE);
                else if (was == HIT_SYS)
                    sysmenu_drop(hwnd);
            }
            return 1;
        }
        return 0;

    case WM_LBUTTONDBLCLK:
        pt.x = GET_X_LPARAM(lParam);
        pt.y = GET_Y_LPARAM(lParam);
        if (cap_button_at(hwnd, pt) == HIT_SYS) {
            PostMessage(hwnd, WM_CLOSE, 0, 0);
            return 1;
        }
        return 0;
    }
    return 0;
}
