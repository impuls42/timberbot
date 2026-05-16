// TimberbotConfigurator.cs. Bindito DI registration.
//
// Timberborn uses Bindito (a custom DI framework) to wire game services.
// [Context("Game")] means this runs when a game is loaded (not on the main menu).

using Bindito.Core;

namespace Timberbot
{
    [Context("Game")]
    public class TimberbotConfigurator : Configurator
    {
        // Register all Timberbot services with the DI container.
        // Bindito auto-resolves constructor parameters from the game's service registry.
        // AsSingleton() = one instance per game session, destroyed when the game unloads.
        //
        // TimberbotService implements ILoadableSingleton/IUpdatableSingleton, so Bindito
        // automatically calls Load() at game start and UpdateSingleton() every frame.
        // The other classes are plain singletons injected into TimberbotService.
        public override void Configure()
        {
            Bind<TimberbotEntityRegistry>().AsSingleton();
            Bind<TimberbotReadV2>().AsSingleton();
            Bind<TimberbotEvents>().AsSingleton();
            Bind<TimberbotWrite>().AsSingleton();
            Bind<TimberbotPlacement>().AsSingleton();
            Bind<TimberbotDebug>().AsSingleton();
            Bind<TimberbotService>().AsSingleton();
            Bind<TimberbotPanel>().AsSingleton();
        }
    }
}
