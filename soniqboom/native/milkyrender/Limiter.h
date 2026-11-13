/* SPDX-FileCopyrightText: 2026 S.F. Cyris
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Limiter.h for SoniqBoom's build of MilkyPlay (soniqboom/native/milkyrender),
 * in place of MilkyPlay's own: the mastering limiter MasterMixer holds, as a
 * pass-through — milkyrender mixes without a limiter. */

#ifndef MILKYRENDER_LIMITER_H
#define MILKYRENDER_LIMITER_H

#include "Mixable.h"

struct Limiter : public Mixable
{
	float ingain;

	Limiter() : ingain(1.0f) {}

	void init(mp_uint32, mp_uint32) {}

	virtual void mix(mp_sint32*, mp_uint32) {}
};

#endif
