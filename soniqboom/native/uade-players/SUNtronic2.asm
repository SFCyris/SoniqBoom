; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (C) 2026 SoniqBoom contributors
;
; SUNtronic2 -- a uade (DeliTracker-style) player for the position-independent
; build of Felix Schmidt's SUNtronic replayer: a module that starts with
; 48E7FFFE 4DFA xxxx 4A2E07C0 (movem.l d0-d7/a0-a6,-(sp); lea xxxx(pc),a6;
; tst.b $7C0(a6)) and keeps its variables at fixed offsets from that A6.
; The module's own code plays the music; this player sets up the module's
; variables, calls the module once per tick and reports the song end when the
; module stops or its position counter goes back.
;
; Build: vasmm68k_mot -Fhunkexe -kick1hunks -nosym -o SUNtronic2 SUNtronic2.asm
dtg_ChkData	EQU	$24
dtg_GetListData	EQU	$38
dtg_AudioAlloc	EQU	$4C
dtg_AudioFree	EQU	$50
dtg_SongEnd	EQU	$5C
DTP_PlayerVersion EQU	$80004458
DTP_PlayerName	EQU	$80004459
DTP_Creator	EQU	$8000445A
DTP_Check2	EQU	$8000445C
DTP_Interrupt	EQU	$8000445E
DTP_InitPlayer	EQU	$80004463
DTP_EndPlayer	EQU	$80004464
DTP_InitSound	EQU	$80004465
DTP_EndSound	EQU	$80004466

V_TICK	EQU	$7bc
V_ROW	EQU	$7bd
V_SPEED	EQU	$7be
V_STOP	EQU	$7c0
V_VOL	EQU	$7c1
V_FADE	EQU	$7c2
V_POS	EQU	$7c6
V_VOICES EQU	$7c8
V_POSTAB EQU	$7d0
V_POSREL EQU	$7d4

	SECTION	sun2,CODE
	moveq	#-1,d0
	rts
	dc.b	'EPPLAYER'
	dc.l	tags
	dc.b	'$VER: SUNtronic2 player 1.0 (SoniqBoom)',0
	even
tags	dc.l	DTP_PlayerVersion,1
	dc.l	DTP_PlayerName,pname
	dc.l	DTP_Creator,pcreator
	dc.l	DTP_Check2,Check2
	dc.l	DTP_Interrupt,Interrupt
	dc.l	DTP_InitPlayer,InitPlayer
	dc.l	DTP_EndPlayer,EndPlayer
	dc.l	DTP_InitSound,InitSound
	dc.l	DTP_EndSound,EndSound
	dc.l	0
pname	dc.b	'SUNtronic',0
pcreator dc.b	'Felix Schmidt, adapted by SoniqBoom',0
	even
modptr	dc.l	0
a6base	dc.l	0
delibase dc.l	0
lastpos	dc.w	0

Check2	move.l	dtg_ChkData(a5),a0
	moveq	#-1,d0
	cmp.l	#$48E7FFFE,(a0)
	bne.b	.no
	cmp.w	#$4DFA,4(a0)
	bne.b	.no
	cmp.l	#$4A2E07C0,8(a0)
	bne.b	.no
	moveq	#0,d0
.no	rts

InitPlayer
	move.l	a5,delibase
	moveq	#0,d0
	move.l	dtg_GetListData(a5),a0
	jsr	(a0)
	move.l	a0,modptr
	move.w	6(a0),d1		; lea disp
	ext.l	d1
	lea	6(a0,d1.l),a1		; A6 as the module computes it
	move.l	a1,a6base
	move.l	dtg_AudioAlloc(a5),a0
	jmp	(a0)

EndPlayer
	move.l	dtg_AudioFree(a5),a0
	jmp	(a0)

InitSound
	movem.l	d0-d7/a0-a6,-(sp)
	move.l	a6base(pc),a6
	lea	voices,a0
	move.l	a0,V_VOICES(a6)
	move.w	#($130*4/4)-1,d0
.clr	clr.l	(a0)+
	dbra	d0,.clr
	lea	voices,a0
	moveq	#3,d0
.v	move.b	#$ff,$14(a0)
	lea	$130(a0),a0
	dbra	d0,.v
	move.l	V_POSREL(a6),d0
	add.l	a6,d0
	move.l	d0,V_POSTAB(a6)
	move.b	V_SPEED(a6),d0
	subq.b	#1,d0
	move.b	d0,V_TICK(a6)
	move.b	#$0f,V_ROW(a6)
	move.w	#$ffff,V_POS(a6)
	clr.b	V_STOP(a6)
	move.b	#$40,V_VOL(a6)
	clr.b	V_FADE(a6)
	clr.w	lastpos
	movem.l	(sp)+,d0-d7/a0-a6
	rts

EndSound
	move.w	#$f,$dff096
	rts

Interrupt
	movem.l	d1-d7/a0-a6,-(sp)
	move.l	modptr(pc),a0
	jsr	(a0)
	move.l	a6base(pc),a6
	tst.b	V_STOP(a6)
	bne.b	.end
	move.w	V_POS(a6),d0
	cmp.w	lastpos(pc),d0
	bcc.b	.ok
.end	move.l	delibase(pc),a5
	move.l	dtg_SongEnd(a5),a0
	jsr	(a0)
	move.l	a6base(pc),a6
	move.w	V_POS(a6),d0
.ok	move.w	d0,lastpos
	movem.l	(sp)+,d1-d7/a0-a6
	moveq	#0,d0
	rts

	SECTION	sun2bss,BSS_C
voices	ds.b	$130*4
