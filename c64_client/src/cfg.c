/**
 * LLM64 Client - persistent network config (see cfg.h)
 */

#include <string.h>
#include <cbm.h>
#include "cfg.h"
#include "serial.h"

/* music.s - the 60Hz tick is starved through an IEC transfer anyway
   (interrupts stay off to hold the bit-banged timing), so hold the tune
   rather than let it warble. Declared here as main.c does; music.s has
   no header of its own. */
void music_hold_begin(void);
void music_hold_end(void);

#ifndef SERVER_IP
#define SERVER_IP   "192.168.1.39"
#endif
#ifndef SERVER_PORT
#define SERVER_PORT "6400"
#endif

/* On-disk layout: magic, version, the two NUL-terminated fields at
   fixed offsets, then (v2+) one baud-index byte. v1 = 40 bytes,
   v2 = 41. config_load reads either; config_save always writes v2.
   The inline .cfg generators (top Makefile inject-cfg / deploy-diag,
   emu/test_e2e.py, emu/repro_menu.py) still emit the v1 prefix, which
   is exactly why v1 must stay loadable. */
#define CFG_MAGIC   0xC6
#define CFG_VERSION 0x02
typedef struct {
    uint8_t magic;
    uint8_t version;
    char host[CFG_HOST_MAX];
    char port[CFG_PORT_MAX];
    uint8_t baud_idx;       /* v2+; absent in v1 blobs (default applied) */
} NetConfig;

/* Size of a v1 blob: everything up to but not including baud_idx. */
#define CFG_V1_SIZE (sizeof(NetConfig) - 1)

char g_host[CFG_HOST_MAX] = SERVER_IP;
char g_port[CFG_PORT_MAX] = SERVER_PORT;
char g_dial[CFG_HOST_MAX + CFG_PORT_MAX + 6];
uint8_t g_baud_idx = BAUD_IDX_DEFAULT;

/* acia_init_hw reads this (serial.s DATA byte) at init. Index -> 6551
   control register: bit4 = internal baud generator, low nibble = rate. */
extern uint8_t acia_ctrl;
static const uint8_t baud_ctrl[3] = { 0x1C, 0x1E, 0x1F };

void baud_apply(void) {
    if (g_baud_idx > BAUD_IDX_38400) g_baud_idx = BAUD_IDX_DEFAULT;
    acia_ctrl = baud_ctrl[g_baud_idx];
}

uint16_t baud_nominal_div100(void) {
    /* 4800/100=48, 9600/100=96, 19200/100=192 */
    static const uint16_t div100[3] = { 48, 96, 192 };
    return div100[g_baud_idx > BAUD_IDX_38400 ? BAUD_IDX_DEFAULT : g_baud_idx];
}

uint8_t boot_device = 8;

void boot_device_init(void) {
    /* $BA = the KERNAL's current device, set by the LOAD that started
       us. Snapshot before any other I/O touches it. */
    uint8_t d = *(volatile uint8_t*)0xBA;
    boot_device = (d >= 8 && d <= 30) ? d : 8;
}

static NetConfig blob;

uint8_t config_load(void) {
    /* cbm_load returns bytes read: 41 for a v2 blob, 40 for a v1 one
       (the baud byte is simply absent). Anything shorter is corrupt. */
    unsigned int n = cbm_load("llm64.cfg", boot_device, &blob);
    if (n < CFG_V1_SIZE) return 0;
    if (blob.magic != CFG_MAGIC) return 0;
    if (blob.version == 0x01 || n < sizeof(blob)) {
        blob.baud_idx = BAUD_IDX_DEFAULT;   /* field not on disk */
    } else if (blob.version != CFG_VERSION) {
        return 0;                            /* unknown newer format */
    }
    blob.host[CFG_HOST_MAX - 1] = 0;
    blob.port[CFG_PORT_MAX - 1] = 0;
    if (!blob.host[0] || !blob.port[0]) return 0;
    strcpy(g_host, blob.host);
    strcpy(g_port, blob.port);
    g_baud_idx = blob.baud_idx;
    baud_apply();               /* clamps a bad index, sets acia_ctrl */
    return 1;
}

uint8_t config_save(void) {
    uint8_t ok;
    memset(&blob, 0, sizeof(blob));
    blob.magic = CFG_MAGIC;
    blob.version = CFG_VERSION;
    strcpy(blob.host, g_host);
    strcpy(blob.port, g_port);
    blob.baud_idx = g_baud_idx;
    /* Bracketed exactly as mod_open() brackets a module load, and for
       the same reason: IEC transfers are bit-banged with cycle-exact
       timing (JiffyDOS especially) and a serial NMI landing mid-byte
       corrupts them.
       The first-boot save is safe without this - nothing is connected
       yet, so no bytes arrive to fire an NMI - but F1 -> e)config ->
       save while CONNECTED is not, and that is the ordinary way to
       change the server address. A mangled cfg is only annoying (the
       editor comes back next boot); a mangled BLOCK is not, because
       this writes to the same disk that holds the overlay modules.
       "@0:" scratches any existing file first (a plain save would fail
       with FILE EXISTS the second time). */
    music_hold_begin();
    serial_rx_pause();
    ok = cbm_save("@0:llm64.cfg", boot_device, &blob,
                  sizeof(blob)) == 0;
    serial_rx_resume();
    music_hold_end();
    return ok;
}

void build_dial_string(void) {
    strcpy(g_dial, "ATDT");
    strcat(g_dial, g_host);
    strcat(g_dial, ":");
    strcat(g_dial, g_port);
}
