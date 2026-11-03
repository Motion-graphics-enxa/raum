/** The public RAUM website: the landing page alone, its calls to action opening the waitlist. No box, no API but /api/waitlist. */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../styles/base.css";
import Landing from "../screens/Landing";
import { WaitlistProvider } from "./Waitlist";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <WaitlistProvider>
      <Landing />
    </WaitlistProvider>
  </StrictMode>,
);
