import { Suspense } from "react";
import { RegisterFlow } from "@/components/register-flow";

export const metadata = {
  title: "Finish setup",
};

export default function RegisterPage() {
  return (
    <div className="mx-auto flex min-h-screen max-w-[560px] flex-col justify-center px-8 py-16">
      <Suspense fallback={null}>
        <RegisterFlow />
      </Suspense>
    </div>
  );
}
