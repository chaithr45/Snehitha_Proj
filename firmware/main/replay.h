// Replay mode (CONFIG_SAFETY_REPLAY): feed the synthetic stream in replay_data.h
// through the real pipeline and check the events against the Python device model.
#pragma once

void replay_start(void);
