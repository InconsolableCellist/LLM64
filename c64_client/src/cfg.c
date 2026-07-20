/**
 * C64 LLM Client - persistent network config (see cfg.h)
 */

#include <string.h>
#include <cbm.h>
#include "cfg.h"

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

static NetConfig blob;

uint8_t config_load(void) {
    if (cbm_load("c64llm.cfg", 8, &blob) != sizeof(blob)) return 0;
    if (blob.magic != CFG_MAGIC || blob.version != CFG_VERSION) return 0;
    blob.host[CFG_HOST_MAX - 1] = 0;
    blob.port[CFG_PORT_MAX - 1] = 0;
    if (!blob.host[0] || !blob.port[0]) return 0;
    strcpy(g_host, blob.host);
    strcpy(g_port, blob.port);
    return 1;
}

uint8_t config_save(void) {
    memset(&blob, 0, sizeof(blob));
    blob.magic = CFG_MAGIC;
    blob.version = CFG_VERSION;
    strcpy(blob.host, g_host);
    strcpy(blob.port, g_port);
    /* "@0:" scratches any existing file first (plain save would fail
       with FILE EXISTS the second time) */
    return cbm_save("@0:c64llm.cfg", 8, &blob, sizeof(blob)) == 0;
}

void build_dial_string(void) {
    strcpy(g_dial, "ATDT");
    strcat(g_dial, g_host);
    strcat(g_dial, ":");
    strcat(g_dial, g_port);
}
