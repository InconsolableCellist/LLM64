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

/* What CallWindowProc will accept for the proc a subclass displaced.
   Win16 declares it FARPROC - int (far pascal *)() - and Win32 declares
   it WNDPROC; each rejects the other's spelling outright. */
typedef FARPROC LlmOldProc;

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

/* Win32 passes the two window handles instead of a flag: lParam is the
   child being activated, so compare it against ourselves. */
#define LLM_MDI_ACTIVE(w, l, self)  ((HWND)(l) == (self))

/* An HWND is 32 bits here, so the Win16 spelling - (HWND)(WORD)result -
   would keep the low half of a real handle and throw the rest away.
   That compiles without complaint and hands you a window that does not
   exist, which is the whole reason this macro is not just a cast. */
#define LLM_HWND(r)     ((HWND)(r))

typedef WNDPROC LlmOldProc;     /* see the Watcom branch above */

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

#endif /* LLMPORT_H */
