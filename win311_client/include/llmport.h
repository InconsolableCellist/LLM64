/*
 * LLM64 for Windows - the seam between the two targets
 *
 * One source tree builds twice:
 *
 *   Open Watcom  -> LLM64.EXE, 16-bit NE, real WfW 3.11 and Win95/98
 *   mingw-w64    -> LLM32.EXE, 32-bit PE, Windows 10/11 and Wine
 *
 * Everything that differs between them lives in this file, because the
 * alternative is two copies of main.c and the 16-bit one rots. If you
 * are adding code, write it in the Win16 spelling and teach this header
 * the Win32 equivalent - not the other way round. The 3.11 build is the
 * point of the exercise.
 *
 * What is NOT here, because mingw already provides it and the 16-bit
 * spelling compiles unchanged: GlobalAlloc/GlobalLock/GlobalFree,
 * OpenFile/_lopen/_lread/_lclose and the OF_* flags (kept in kernel32
 * as 16-bit compatibility), MakeProcInstance/FreeProcInstance (already
 * no-op macros in winbase.h), lstrcpy/lstrcpyn/lstrlen, wsprintf,
 * FAR/PASCAL, and GetWindowLong/SetWindowLong.
 *
 * Do NOT call this file win16.h, however obvious that name looks.
 * Watcom's own <windows.h> is a two-line dispatcher:
 *
 *     #ifdef _WINDOWS_16_
 *         #include <win16.h>
 *     #else
 *         #include <_win386.h>
 *     #endif
 *
 * so a win16.h of ours anywhere on the -I path shadows the real 16-bit
 * API header, and windows.h quietly includes the wrong file. The symptom
 * is a pile of redefinition errors inside _win386.h - a header that has
 * no business being in a 16-bit build at all, which is the clue.
 *
 * Target the 32-bit PE, not 64-bit: WPARAM is then exactly the UINT the
 * Win16 window procs already declare, so all fourteen of them compile
 * for both targets with no signature change, and the LONG casts of View
 * pointers stay legal. WoW64 is on every x64 Windows and Windows 11 on
 * ARM emulates x86-32, so nothing is lost by it.
 */

#ifndef LLMPORT_H
#define LLMPORT_H

#include <windows.h>    /* so this header stands on its own */

#ifdef __WATCOMC__
/* ---------------------------------------------------------------- 16 */

#include <i86.h>        /* FP_OFF, for segment-boundary math */

/* All three are real and load-bearing here: _export emits the prologue
   that reloads DS in a callback, __huge makes pointer arithmetic carry
   across a segment boundary, and _fmemcpy takes far pointers. */

#define LLM_INST(w)     ((HINSTANCE)GetWindowWord((w), GWW_HINSTANCE))

/* WM_COMMAND in Win16: wParam is the id, lParam packs the control's
   window handle and the notification code. */
#define LLM_CMD_ID(w, l)        (w)
#define LLM_CMD_NOTIFY(w, l)    HIWORD(l)
#define LLM_CMD_HWND(w, l)      ((HWND)LOWORD(l))

/* WM_VSCROLL/WM_HSCROLL: code in wParam, thumb position in lParam. */
#define LLM_SCROLL_CODE(w, l)   (w)
#define LLM_SCROLL_POS(w, l)    LOWORD(l)

/* WM_MDIACTIVATE: wParam is a flag saying whether THIS child is the one
   being activated, which is the only thing either caller wants. */
#define LLM_MDI_ACTIVE(w, l, self)  ((int)(w))

/* A window handle came back from SendMessage in the low word, because a
   Win16 handle IS a word. */
#define LLM_HWND(r)     ((HWND)(WORD)(r))

/* The control-colour messages. 3.1 has exactly one, WM_CTLCOLOR, and
   says which kind of control is asking in the high word of lParam. */
#define LLM_IS_CTLCOLOR(m)      ((m) == WM_CTLCOLOR)
#define LLM_CTLCOLOR_KIND(m, l) ((int)HIWORD(l))
#define LLM_CTLCOLOR_DC(w)      ((HDC)(w))

/* Win32 added these TrackPopupMenu flags; 3.1 aligns a popup to the top
   of the given point anyway, so asking for it is a no-op. */
#define TPM_TOPALIGN    0
#define TPM_BOTTOMALIGN 0

/* windowsx.h is where Win32 keeps these; 3.1 predates it. Signed, because
   a mouse can be dragged off the left edge of a window and an unsigned
   read turns -3 into 65533. */
#define GET_X_LPARAM(l) ((int)(short)LOWORD(l))
#define GET_Y_LPARAM(l) ((int)(short)HIWORD(l))

/* Where a maximised window may go. Windows 3.1 has no notion of a work
   area - no taskbar to keep clear of - so it is the whole screen. */
#define LLM_WORKAREA(r) do {                            \
        (r)->left = 0; (r)->top = 0;                    \
        (r)->right  = GetSystemMetrics(SM_CXSCREEN);    \
        (r)->bottom = GetSystemMetrics(SM_CYSCREEN);    \
    } while (0)

/* What CallWindowProc will accept for the proc a subclass displaced.
   Win16 declares it FARPROC - int (far pascal *)() - and Win32 declares
   it WNDPROC; each rejects the other's spelling outright. */
typedef FARPROC LlmOldProc;

/* ShellExecute answers a fake HINSTANCE, and anything above 32 means it
   worked. A handle is a word here, so the compare is one cast wide. */
#define LLM_SHELL_OK(h)     ((UINT)(h) > 32)

#else
/* ---------------------------------------------------------------- 32 */

#include <string.h>     /* memcpy, for _fmemcpy below */

/* Flat memory: the segment vocabulary all becomes nothing. huge_bite()
   and huge_store() still walk their buffers in 16 KB steps, which is
   harmless here - just a loop that runs once per bite. */
#define _export
#define __huge
#define _fmemcpy        memcpy
#define FP_OFF(p)       ((unsigned)(size_t)(p))

#define LLM_INST(w)     ((HINSTANCE)GetWindowLong((w), GWL_HINSTANCE))

/* Win32 moved the notification code up into wParam beside the id, and
   gave lParam the control's handle whole. This is the single most
   common way a Win16 port compiles clean and then misbehaves: every
   menu command still works, because HIWORD(wParam) is 0 for a menu, and
   every control notification silently stops matching. */
#define LLM_CMD_ID(w, l)        LOWORD(w)
#define LLM_CMD_NOTIFY(w, l)    HIWORD(w)
#define LLM_CMD_HWND(w, l)      ((HWND)(l))

#define LLM_SCROLL_CODE(w, l)   LOWORD(w)
#define LLM_SCROLL_POS(w, l)    HIWORD(w)

/* Win32 split WM_CTLCOLOR into six messages, one per kind of control,
   and they are consecutive and in the same order as 3.1's CTLCOLOR_*
   codes - so subtracting the first maps one onto the other exactly.
   Those codes went out of the SDK headers with WINVER 4.0. */
#ifndef CTLCOLOR_MSGBOX
#define CTLCOLOR_MSGBOX     0
#define CTLCOLOR_EDIT       1
#define CTLCOLOR_LISTBOX    2
#define CTLCOLOR_BTN        3
#define CTLCOLOR_DLG        4
#define CTLCOLOR_SCROLLBAR  5
#define CTLCOLOR_STATIC     6
#endif
#define LLM_IS_CTLCOLOR(m)  ((m) >= WM_CTLCOLORMSGBOX && \
                             (m) <= WM_CTLCOLORSTATIC)
#define LLM_CTLCOLOR_KIND(m, l) ((int)((m) - WM_CTLCOLORMSGBOX))
#define LLM_CTLCOLOR_DC(w)      ((HDC)(w))

/* Win32 passes the two window handles instead of a flag: lParam is the
   child being activated, so compare it against ourselves. */
#define LLM_MDI_ACTIVE(w, l, self)  ((HWND)(l) == (self))

/* An HWND is 32 bits here, so the Win16 spelling - (HWND)(WORD)result -
   would keep the low half of a real handle and throw the rest away.
   That compiles without complaint and hands you a window that does not
   exist, which is the whole reason this macro is not just a cast. */
#define LLM_HWND(r)     ((HWND)(r))

/* GET_X_LPARAM / GET_Y_LPARAM come from here on Win32. */
#include <windowsx.h>

/* The monitor's work area, so a maximised window keeps clear of the
   taskbar. Multi-monitor aware, and it needs no frame metrics. */
#define LLM_WORKAREA(r) do {                                            \
        MONITORINFO mi_;                                                \
        mi_.cbSize = sizeof(mi_);                                       \
        if (GetMonitorInfo(MonitorFromWindow(hwnd,                      \
                           MONITOR_DEFAULTTONEAREST), &mi_))            \
            *(r) = mi_.rcWork;                                          \
        else                                                            \
            SetRect((r), 0, 0, GetSystemMetrics(SM_CXSCREEN),           \
                    GetSystemMetrics(SM_CYSCREEN));                     \
    } while (0)

typedef WNDPROC LlmOldProc;     /* see the Watcom branch above */

/* The same fake HINSTANCE, 32 bits wide. Casting straight to UINT would
   warn about a pointer losing precision on the way, so it goes through
   an integer of pointer width first. */
#define LLM_SHELL_OK(h)     ((UINT)(UINT_PTR)(h) > 32)

/* Removed from Win32 in favour of the ...Ex forms. The return value of
   MoveTo was the previous position and no caller here wants it. */
#define MoveTo(hdc, x, y)   MoveToEx((hdc), (x), (y), NULL)

/* GetTextExtent returned width and height packed in a DWORD, and the
   one caller takes LOWORD of it. */
static __inline DWORD llm_text_extent(HDC hdc, LPCSTR s, int n)
{
    SIZE sz;

    if (!GetTextExtentPoint32(hdc, s, n, &sz))
        return 0;
    return MAKELONG(sz.cx, sz.cy);
}
#define GetTextExtent(hdc, s, n)    llm_text_extent((hdc), (s), (n))

#endif /* __WATCOMC__ */

/* ------------------------------------------------------------------ */

/* There was no wheel in 1993, so 3.1 has neither the message nor the
   constant. Defined rather than branched on, so the scrollback's wheel
   handler compiles for both targets; the 16-bit one simply never
   receives it. 0x020A is unused in 3.1, so nothing else can arrive
   wearing this number. */
#ifndef WM_MOUSEWHEEL
#define WM_MOUSEWHEEL   0x020A
#endif
#ifndef WHEEL_DELTA
#define WHEEL_DELTA     120
#endif

/* The 3D window edges, which are Windows 95's and do not exist in 3.1 at
   all. Zero here rather than absent, so the code that takes them back
   off a control compiles for both targets and does nothing on the one
   that never had them. */
#ifndef WS_EX_CLIENTEDGE
#define WS_EX_CLIENTEDGE    0L
#define WS_EX_STATICEDGE    0L
#define WS_EX_WINDOWEDGE    0L
#endif

/* What BM_GETSTATE answers with. Win32 gave the bits names; 3.1
   documented them and left it there, so the 16-bit headers have the
   message but not the vocabulary. Guarded rather than branched: on the
   32-bit side these are already right and this whole block vanishes. */
#ifndef BST_CHECKED
#define BST_UNCHECKED       0x0000
#define BST_CHECKED         0x0001
#define BST_INDETERMINATE   0x0002
#define BST_PUSHED          0x0004
#define BST_FOCUS           0x0008
#endif

#endif /* LLMPORT_H */
