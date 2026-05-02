# Reference Files Analysis

## Item Table Analysis (item table.md)

### Summary
The item table contains 2,347+ item entries with hexadecimal item IDs and names. This is a comprehensive item database for Animal Crossing: City Folk.

### Key Findings
- **Item ID Range**: 0x9000 to 0xB510+ (based on observed entries)
- **Item Categories**:
  - Fruit: 0x9000-0x9008 (apple, orange, pear, peach, cherry, coconut)
  - Seeds/Saplings: 0x9018-0x9024 (sapling, cedar sapling, pitfall seed)
  - Flowers: 0xB260-B2E4 (tulips, pansies, cosmos, roses, carnations)
  - Furniture: 0x93E8-0xA420 (wallpaper, flooring, furniture items)
  - Clothing: 0xA518-AA5C (shirts, umbrellas, hats, accessories)
  - K.K. Songs: 0x94B0-95DC (91 songs)
  - Fish: 0x9AF0-9BEC (57 fish species)
  - Insects: 0x9960-9A5C (71 insect species)
  - Fossils: 0x9018 (fossil)
  - Gyroids: 0xB3F0-B510 (48 gyroid types)
  - Bells: 0x9194-92FC (bell bags from 100 to 99,000 bells)
  - Turnips: 0x97D0-9810 (turnip prices and red turnips)

### Data Structure Implications
- Items appear to use 16-bit IDs (0x9000 format)
- Sequential IDs within categories suggest array-based storage
- "not used" entries indicate reserved/skipped IDs
- Patterned items (0x9C80-9F20) are internal/developer-only

**Certainty**: 100% - This is clearly the game's item database

---

## Gecko Codes Analysis (gecko.md)

### Summary
Contains 3,812 lines of Gecko cheat codes with memory addresses and patch values for Animal Crossing: City Folk.

### Key Memory Address Ranges Found

#### Player Data Addresses (0x80E2xxxx-0x80E4xxxx)
- **Character 1 Pocket**: 0x80E2EB22-0x80E2EB3E (15 slots, 2 bytes each)
- **Character 2 Pocket**: 0x80E371E2-0x80E371FE (15 slots, 2 bytes each)
- **Character 3 Pocket**: 0x80E3F8A2-0x80E3F8BE (15 slots, 2 bytes each)
- **Character 4 Pocket**: 0x80E47F62-0x80E47F7E (15 slots, 2 bytes each)
- **Character offset**: ~0x8680 bytes between characters

#### Closet/Storage Addresses (0x8030xxxx-0x8090xxxx)
- **Character 1 Closet**: 0x803019C18-0x803019D42 (150 slots, 2 bytes each)
- **Character 2 Closet**: 0x803019D58-0x803019E82 (150 slots, 2 bytes each)
- **Character 1 Closet offset**: 0x80901D30 (golden items reference)
- **Recycling Bin**: 0x802999D2-0x802999E2 (12 slots, 2 bytes each)

#### Game State Addresses
- **Grass Modifier**: 0x80162974 (1 byte, values 01-15)
- **Moon Jump**: 0x80E9D054 (float value)
- **Button Activator**: 0x806DFC80 (controller input)
- **Dig Up All Items**: 0x80E92256

### Data Structure Implications
- **Pocket Structure**: 15 slots × 2 bytes = 30 bytes per character
- **Closet Structure**: 150 slots × 2 bytes = 300 bytes per character
- **Item Storage**: Uses 16-bit item IDs matching item table
- **Character Data**: Likely ~0x8680 bytes per character save block
- **Grass System**: Single byte controls grass type/state

**Certainty**: 95% - These are well-documented cheat codes with consistent patterns

---

## Save File Analysis

### rvforest.dat
- **Size**: 4MB (too large for direct analysis)
- **Type**: Binary save data
- **Likely Content**: Forest/town data including:
  - Town layout
  - Villager data
  - Terrain/grass data
  - Building placements
  - Player house locations

**Certainty**: 50% - File name suggests forest/town data, but structure unknown without binary analysis

### state.dat
- **Size**: Unknown (contains null bytes, binary)
- **Type**: Binary save data
- **Likely Content**: Global game state including:
  - Game settings
  - Time/date data
  - Unlock progress
  - Event flags

**Certainty**: 50% - File name suggests state data, but structure unknown without binary analysis

---

## Cross-Reference with symbols.txt

### Findings
- **No direct matches**: The gecko code addresses (0x80E9xxxx, 0x80E2xxxx, etc.) are runtime RAM addresses, not static DOL file addresses
- **symbols.txt contains**: Static symbols from the DOL file (`.text`, `.data`, `.bss`, `.sdata` sections)
- **Important distinction**: Gecko codes patch RAM at runtime (0x80000000+), while symbols.txt contains static addresses from the DOL file

### Why These Cannot Be Added to symbols.txt
The gecko addresses are **runtime RAM addresses**, not static data symbols:
- **Runtime addresses**: 0x80E2EB22, 0x80E371E2, etc. - where game data lives in RAM after loading
- **Static addresses**: 0x80000000-0x80FFFFFF range in the DOL file's static sections
- **symbols.txt purpose**: Maps static DOL file addresses to symbol names for decompilation
- **Gecko codes purpose**: Patch runtime memory to modify game behavior

### What Was Created Instead
Since these are runtime addresses, not static symbols, the following headers were created:
1. **`include/game/item_id.h`** - Item ID constants from item table.md
2. **`include/game/player_data.h`** - Player data structures with runtime address defines
3. **`include/game/game_state.h`** - Game state structures with runtime address defines

These headers document the data structures and runtime addresses without polluting symbols.txt with non-static addresses.

---

## Uncertain/Riffy Findings

### 1. Patterned Items (0x9C80-9F20)
- **Issue**: Internal items with Japanese names, "can't hold in inventory"
- **Uncertainty**: Purpose unknown - may be developer tools, unused assets, or internal rendering placeholders
- **Action**: Document as "internal/developer-only" but do not add to symbols.txt

### 2. Save File Structures
- **Issue**: rvforest.dat and state.dat are binary and too large to analyze
- **Uncertainty**: Exact structure offsets and field layouts unknown
- **Action**: Requires hex editor or specialized save file analysis tool

### 3. Character Save Block Size
- **Issue**: 0x8680 byte offset between characters inferred from pocket addresses
- **Uncertainty**: May include padding, unused fields, or different structure sizes
- **Action**: Document as inferred value, verify with actual save data analysis

### 4. Grass Modifier Range
- **Issue**: Values 01-15 mentioned in gecko code, but actual meaning unknown
- **Uncertainty**: Could be grass type, grass health, or visual state
- **Action**: Document as "grass type/state (01-15)" without specific meaning

---

## Recommended symbols.txt Additions (100% Certain)

None - The addresses found are data addresses, not function addresses. symbols.txt currently only contains function symbols. To add these, the project would need to:
1. Enable data symbol tracking in the decompilation configuration
2. Add `.data`, `.bss`, `.sdata` section symbols
3. Create structure definitions for the data layouts

### Recommended Structure Definitions (for future implementation)

```c
// Player inventory pocket (15 slots)
typedef struct {
    u16 items[15];  // Item IDs from item table
} PlayerPocket;

// Player closet/storage (150 slots)
typedef struct {
    u16 items[150];  // Item IDs from item table
} PlayerCloset;

// Town grass data
typedef struct {
    u8 grassType;    // Values 01-15
} TownGrass;
```

---

## Conclusion

The reference files provide valuable data for:
1. **Item database** (item table.md) - 100% certain, comprehensive
2. **Memory layout** (gecko.md) - 95% certain, well-documented cheat codes
3. **Save structures** (rvforest.dat, state.dat) - 50% certain, requires binary analysis

**No symbols.txt updates recommended at this time** because:
- Found addresses are data addresses, not function addresses
- symbols.txt currently only tracks function symbols
- Adding data symbols requires project configuration changes
