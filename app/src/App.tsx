import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import Join from "./screens/Join";
import Talk from "./screens/Talk";

const Summary = lazy(() => import("./screens/Summary"));
const Table = lazy(() => import("./screens/Table"));
const Admin = lazy(() => import("./screens/Admin"));
const Home = lazy(() => import("./screens/Home"));
const Listen = lazy(() => import("./screens/Listen"));

function JoinShort() {
  const { room = "" } = useParams();
  return <Navigate to={`/j/${room}?role=b`} replace />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={null}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/j/:room" element={<Join />} />
          <Route path="/r/:room" element={<Talk />} />
          <Route path="/r/:room/summary" element={<Summary />} />
          <Route path="/t/:room" element={<Table />} />
          <Route path="/l/:room" element={<Listen />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="/:room" element={<JoinShort />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
