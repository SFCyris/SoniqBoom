/*
 * Copyright (c) 2009, The MilkyTracker Team.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * - Redistributions of source code must retain the above copyright notice,
 *   this list of conditions and the following disclaimer.
 * - Redistributions in binary form must reproduce the above copyright
 *   notice, this list of conditions and the following disclaimer in the
 *   documentation and/or other materials provided with the distribution.
 * - Neither the name of the <ORGANIZATION> nor the names of its contributors
 *   may be used to endorse or promote products derived from this software
 *   without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */

/*
 *  ResamplerFactory.cpp
 *  MilkyPlay
 *
 *  Created by Peter Barth on 08.11.07.
 *
 *  SPDX-License-Identifier: BSD-3-Clause
 *  SoniqBoom's build of MilkyPlay (soniqboom/native/milkyrender) uses this
 *  file in place of MilkyPlay's own ResamplerFactory.cpp: the same factory
 *  with the resamplers of ResamplerFast.h only (no interpolation, linear
 *  interpolation, each with or without volume ramping); asking for any other
 *  type gives NULL, as for a type the factory doesn't make.
 */

#include "ResamplerFactory.h"
#include "ResamplerFast.h"

ChannelMixer::ResamplerBase* ResamplerFactory::createResampler(ResamplerTypes type)
{
	switch (type)
	{
		case MIXER_NORMAL:
			return new ResamplerSimple();

		case MIXER_NORMAL_RAMPING:
			return new ResamplerSimpleRamp();

		case MIXER_LERPING:
			return new ResamplerLerp();

		case MIXER_LERPING_RAMPING:
			return new ResamplerLerpRampFilter();

		case MIXER_DUMMY:
			return new ResamplerDummy();

		default:
			return NULL;
	}
}
