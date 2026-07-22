/**
 * C64 LLM Client - persistent network config (see cfg.h)
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

/* On-disk layout: magic, version, then the two NUL-terminated fields at
   fixed offsets. 40 bytes total. The Makefile's default.cfg generator
   (build/default.cfg) must match this layout byte for byte. */
#define CFG_MAGIC   0xC6
#define CFG_VERSION 0x01
typedef struct {
    uint8_t magic;
    uint8_t version;
    char host[CFG_HOST_MAX];
    char port[CFG_PORT_MAX];
} NetConfig;

char g_host[CFG_HOST_MAX] = SERVER_IP;
char g_port[CFG_PORT_MAX] = SERVER_PORT;
char g_dial[CFG_HOST_MAX + CFG_PORT_MAX + 6];

uint8_t boot_device = 8;

void boot_device_init(void) {
    /* $BA = the KERNAL's current device, set by the LOAD that started
       us. Snapshot before any other I/O touches it. */
    uint8_t d = *(volatile uint8_t*)0xBA;
    boot_device = (d >= 8 && d <= 30) ? d : 8;
}

static NetConfig blob;

uint8_t config_load(void) {
    if (cbm_load("c64llm.cfg", boot_device, &blob) != sizeof(blob)) return 0;
    if (blob.magic != CFG_MAGIC || blob.version != CFG_VERSION) return 0;
    blob.host[CFG_HOST_MAX - 1] = 0;
    blob.port[CFG_PORT_MAX - 1] = 0;
    if (!blob.host[0] || !blob.port[0]) return 0;
    strcpy(g_host, blob.host);
    strcpy(g_port, blob.port);
    return 1;
}

uint8_t config_save(void) {
    uint8_t ok;
    memset(&blob, 0, sizeof(blob));
    blob.magic = CFG_MAGIC;
    blob.version = CFG_VERSION;
    strcpy(blob.host, g_host);
    strcpy(blob.port, g_port);
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
    ok = cbm_save("@0:c64llm.cfg", boot_device, &blob,
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
