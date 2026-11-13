/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Stand-ins for the SDL 1.2 functions GoatTracker's shared code references,
 * linked into gt2reloc in place of the SDL library.  gt2reloc (a command-line
 * packer) never calls any of them — it opens no window, no audio device and
 * reads no input — but its object files name them: GoatTracker's makefile
 * links the tracker's display, sound and BME library code into it too.
 * Linking SDL itself makes gt2reloc load SDL2 at start (sdl12-compat), which
 * on macOS stalled in its library initializer before main ran.  Each stand-in
 * fails or does nothing, as SDL does without a display or audio device.
 * Compiled against SDL 1.2's headers (`sdl-config --cflags`), so every
 * signature is the header's. */

#include <SDL.h>

int SDL_Init(Uint32 flags) { (void)flags; return -1; }
void SDL_Quit(void) {}
void SDL_Delay(Uint32 ms) { (void)ms; }
Uint32 SDL_GetTicks(void) { return 0; }
SDL_Surface *SDL_SetVideoMode(int w, int h, int bpp, Uint32 flags) {
  (void)w; (void)h; (void)bpp; (void)flags; return NULL;
}
void SDL_WM_SetCaption(const char *title, const char *icon) { (void)title; (void)icon; }
void SDL_WM_SetIcon(SDL_Surface *icon, Uint8 *mask) { (void)icon; (void)mask; }
void SDL_UpdateRect(SDL_Surface *s, Sint32 x, Sint32 y, Uint32 w, Uint32 h) {
  (void)s; (void)x; (void)y; (void)w; (void)h;
}
int SDL_LockSurface(SDL_Surface *s) { (void)s; return -1; }
void SDL_UnlockSurface(SDL_Surface *s) { (void)s; }
int SDL_ShowCursor(int toggle) { (void)toggle; return 0; }
int SDL_SetTimer(Uint32 interval, SDL_TimerCallback cb) { (void)interval; (void)cb; return -1; }
int SDL_SetColors(SDL_Surface *s, SDL_Color *c, int first, int n) {
  (void)s; (void)c; (void)first; (void)n; return 0;
}
SDL_RWops *SDL_RWFromMem(void *mem, int size) { (void)mem; (void)size; return NULL; }
void SDL_PumpEvents(void) {}
int SDL_PollEvent(SDL_Event *e) { (void)e; return 0; }
void SDL_PauseAudio(int pause_on) { (void)pause_on; }
int SDL_OpenAudio(SDL_AudioSpec *want, SDL_AudioSpec *got) { (void)want; (void)got; return -1; }
void SDL_CloseAudio(void) {}
SDL_Surface *SDL_LoadBMP_RW(SDL_RWops *src, int freesrc) { (void)src; (void)freesrc; return NULL; }
int SDL_Flip(SDL_Surface *s) { (void)s; return -1; }
int SDL_EnableUNICODE(int enable) { (void)enable; return 0; }
int SDL_EnableKeyRepeat(int delay, int interval) { (void)delay; (void)interval; return -1; }
