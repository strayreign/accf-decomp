#ifndef GAME_STATE_H
#define GAME_STATE_H

#include "../dolphin/types.h"

// Town grass data
// Values 01-15 based on gecko.md analysis
typedef struct {
    u8 grassType;  // Grass type/state (01-15)
} TownGrass;

// Memory addresses from gecko.md analysis
#define ADDR_GRASS_MODIFIER 0x80162974
#define ADDR_MOON_JUMP 0x80E9D054
#define ADDR_DIG_UP_ALL_ITEMS 0x80E92256
#define ADDR_BUTTON_ACTIVATOR 0x806DFC80

// Grass type values (inferred from gecko code)
#define GRASS_TYPE_MIN 0x01
#define GRASS_TYPE_MAX 0x0F

#endif // GAME_STATE_H
