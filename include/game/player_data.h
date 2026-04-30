#ifndef PLAYER_DATA_H
#define PLAYER_DATA_H

#include "item_id.h"
#include "../dolphin/types.h"

// Player pocket inventory (15 slots)
// Each slot is 2 bytes (u16 item ID)
#define POCKET_SLOT_COUNT 15

typedef struct {
    u16 items[POCKET_SLOT_COUNT];  // Item IDs from item_id.h
} PlayerPocket;

// Player closet/storage (150 slots)
// Each slot is 2 bytes (u16 item ID)
#define CLOSET_SLOT_COUNT 150

typedef struct {
    u16 items[CLOSET_SLOT_COUNT];  // Item IDs from item_id.h
} PlayerCloset;

// Recycling bin (12 slots)
// Each slot is 2 bytes (u16 item ID)
#define RECYCLING_BIN_SLOT_COUNT 12

typedef struct {
    u16 items[RECYCLING_BIN_SLOT_COUNT];  // Item IDs from item_id.h
} RecyclingBin;

// Memory addresses from gecko.md analysis
// These are RAM addresses where player data is stored
#define ADDR_PLAYER1_POCKET 0x80E2EB22
#define ADDR_PLAYER2_POCKET 0x80E371E2
#define ADDR_PLAYER3_POCKET 0x80E3F8A2
#define ADDR_PLAYER4_POCKET 0x80E47F62

#define ADDR_PLAYER1_CLOSET 0x803019C18
#define ADDR_PLAYER2_CLOSET 0x803019D58

#define ADDR_RECYCLING_BIN 0x802999D2

// Character save block offset (inferred from pocket addresses)
#define CHARACTER_SAVE_BLOCK_SIZE 0x8680

#endif // PLAYER_DATA_H
