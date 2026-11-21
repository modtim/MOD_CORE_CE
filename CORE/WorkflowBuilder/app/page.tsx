import WorkflowLoader from "@/components/workflow-loader";

export default function Home() {
  return (
    <main id="wb-main-content" className="flex min-h-screen flex-col">
      <div className="flex-1">
        <WorkflowLoader />
      </div>
    </main>
  );
}