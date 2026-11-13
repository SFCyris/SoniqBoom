# uade players bundled with SoniqBoom

Players for uade123 that SoniqBoom passes with `-P <player>`.

## SUNtronic2

`SUNtronic2` plays SUNtronic modules (`.sun`, `sun.*`) of the
position-independent build of Felix Schmidt's SUNtronic replayer: a module
that starts with the bytes `48E7FFFE 4DFA xxxx 4A2E07C0`. uade's own
SUN-Tronic player (Wanted Team) refuses these modules. SoniqBoom uses
`SUNtronic2` for a SUNtronic module only when uade's own player refuses it
and `SUNtronic2` accepts it (`uade123 -g`); every other SUNtronic module plays
with uade's own player.

The module's own code plays the music. `SUNtronic2` sets up the module's
variables, calls the module once per tick and reports the song end when the
module stops or its position counter goes back. It reports its format as
`SUNtronic`, like uade's own player.

| File | What it is |
|---|---|
| `SUNtronic2.asm` | Source (Motorola 68000 assembly) |
| `SUNtronic2` | The built player (AmigaOS hunk executable, 596 bytes) |

SHA-256 of `SUNtronic2`:
`70003b62837f5bbaa7a49a3a7bc7463d052bb1f65ea188a7919bcd68564dde4f`

### Building

With [vasm](http://sun.hasenbraten.de/vasm/) 1.8i (the `vasmm68k_mot`
build: Motorola syntax, 680x0 CPU module):

```
vasmm68k_mot -Fhunkexe -kick1hunks -nosym -o SUNtronic2 SUNtronic2.asm
```

`-nosym` leaves the symbol table out; with it the build is byte-for-byte
reproducible (the SHA-256 above).

### Licence

AGPL-3.0-or-later, like SoniqBoom (see `LICENSE` at the repository root).
