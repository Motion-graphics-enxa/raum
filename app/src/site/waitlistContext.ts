import { createContext, useContext } from "react";

/**
 * On the public website (`vite build --mode site`) there is no box to open a room on, so the landing's calls to
 * action open the waitlist instead. The site entry provides this; in the app it stays null and they link to /start.
 */
export const WaitlistContext = createContext<(() => void) | null>(null);
export const useWaitlist = () => useContext(WaitlistContext);
