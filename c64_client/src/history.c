/**
 * LLM64 Client - recall of the last sent message (SOFT80)
 *
 * A message eaten by a lost frame is one you retype, and they get long.
 * Up-arrow on an empty line brings the last one back.
 *
 * It lives in the hole between the soft-80 shadow ($C000, 2000 bytes)
 * and the color matrix ($CC00): 1072 bytes of VIC bank 3 that nothing
 * else uses. Free RAM is why this exists at all - BSS has 55 bytes left
 * before the overlay slot, and every byte of CODE here comes out of
 * that same 55, because BSS starts where the code ends.
 *
 * One slot, not a ring. A ring wanted an index and 16-bit packing
 * arithmetic, and cc65 charged about a kilobyte of code for it - which
 * is 987 bytes more than exists. Truncating entries to fit several
 * would have defeated the point, since it is the LONG message you mind
 * retyping. So: one message, all 960 characters of it.
 */

#ifdef SOFT80

#include <string.h>
#include "common.h"
#include "history.h"

#define HIST_TEXT  ((char*)0xC802)          /* $C802-$CBC1, 960 bytes */
#define HIST_LEN   (*(uint16_t*)0xC800)
#define HIST_FLAG  (*(uint8_t*)0xC7D0)      /* set once we have written */
#define HIST_WALK  (*(uint8_t*)0xC7D1)      /* recall showing */
#define HIST_MAGIC 0x6C

/* Raw RAM, not BSS, so it is never zeroed: a magic byte says whether
   what is sitting there is ours or the previous session's rubbish. */
void hist_init(void) {
    if (HIST_FLAG != HIST_MAGIC) {
        HIST_FLAG = HIST_MAGIC;
        HIST_LEN = 0;
    }
    HIST_WALK = 0;
}

void hist_add(const char* text, uint16_t n) {
    if (!n) return;
    if (n > 960) n = 960;
    memcpy(HIST_TEXT, text, n);
    HIST_LEN = n;
    HIST_WALK = 0;
}

const char* hist_prev(uint16_t* n) {
    if (HIST_FLAG != HIST_MAGIC || HIST_LEN == 0) return 0;
    HIST_WALK = 1;
    *n = HIST_LEN;
    return HIST_TEXT;
}

uint8_t hist_walking(void) {
    return HIST_WALK;
}

void hist_reset(void) {
    HIST_WALK = 0;
}

#endif /* SOFT80 */
