using System;
using System.Collections.Generic;
using Timberborn.BlockSystem;
using Timberborn.Bots;
using Timberborn.EntitySystem;
using Timberborn.Forestry;
using Timberborn.Goods;
using Timberborn.PrioritySystem;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace Timberbot
{
    // TimberbotEntityRegistry.cs. Entity lookup and ID translation.
    //
    // WHY TWO ID SYSTEMS
    // -------------------
    // Timberborn internally identifies entities by GUID (EntityComponent.EntityId).
    // But GUIDs are terrible for humans typing API calls ("set_workers id:abc123-...").
    // So the public API uses deterministic hashes of the stable EntityId GUID.
    //
    // Also holds shared constants (faction suffix, species lists, priority names)
    // and the EventBus lifecycle hooks that keep ReadV2's tracked refs in sync.
    public class TimberbotEntityRegistry
    {
        private readonly EntityRegistry _entityRegistry;
        private readonly TreeCuttingArea _treeCuttingArea;
        private readonly EventBus _eventBus;
        private readonly IGoodService _goodService;
        private readonly Dictionary<int, Guid> _legacyToEntityId = new Dictionary<int, Guid>();

        public TimberbotWebhook WebhookMgr;

        public static readonly HashSet<string> TreeSpecies = new HashSet<string>
            { "Pine", "Birch", "Oak", "Maple", "Chestnut", "Mangrove" };
        public static readonly HashSet<string> CropSpecies = new HashSet<string>
            { "Kohlrabi", "Soybean", "Corn", "Sunflower", "Eggplant", "Algae", "Cassava", "Mushroom", "Potato", "Wheat", "Carrot" };
        public static string FactionSuffix = "";
        public static readonly string[] OrientNames = { "south", "west", "north", "east" };
        public static readonly string[] PriorityNames = { "VeryLow", "Low", "Normal", "High", "VeryHigh" };

        public TimberbotEntityRegistry(
            EntityRegistry entityRegistry,
            TreeCuttingArea treeCuttingArea,
            EventBus eventBus,
            IGoodService goodService)
        {
            _entityRegistry = entityRegistry;
            _treeCuttingArea = treeCuttingArea;
            _eventBus = eventBus;
            _goodService = goodService;
        }

        public void Register() => _eventBus.Register(this);
        public void Unregister() => _eventBus.Unregister(this);

        public static string GetPriorityName(Priority p)
        {
            int i = (int)p;
            return (i >= 0 && i < PriorityNames.Length) ? PriorityNames[i] : "Normal";
        }

        public static string CanonicalName(string name) => TimberbotPure.CanonicalName(name);

        public static string CleanName(string name) => TimberbotPure.CleanName(name, FactionSuffix);

        public IReadOnlyList<string> AllGoodIds => _goodService.Goods;

        public bool TreeInCuttingArea(Vector3Int coords) => _treeCuttingArea.IsInCuttingArea(coords);

        public EntityComponent FindEntity(int id)
        {
            if (!TryGetEntityId(id, out var entityId))
                return null;

            var ec = FindEntity(entityId);
            if (ec != null)
                return ec;

            _legacyToEntityId.Remove(id);
            return null;
        }

        public EntityComponent FindEntity(Guid entityId)
            => entityId == Guid.Empty ? null : _entityRegistry.GetEntity(entityId);

        public bool TryGetEntityId(int legacyId, out Guid entityId)
            => _legacyToEntityId.TryGetValue(legacyId, out entityId);

        public bool TryGetLegacyId(Guid entityId, out int legacyId)
        {
            if (entityId == Guid.Empty)
            {
                legacyId = 0;
                return false;
            }
            // Computational instant hash resolution
            int h = Math.Abs(entityId.GetHashCode());
            legacyId = h == 0 ? 1 : h;
            return true;
        }

        public int GetLegacyId(EntityComponent ec)
        {
            int legacyId = ResolvePublicId(ec);
            if (legacyId == 0)
                return 0;

            var entityId = ec.EntityId;
            if (entityId != Guid.Empty)
                _legacyToEntityId[legacyId] = entityId;
            return legacyId;
        }

        public void BuildAllIndexes()
        {
            _legacyToEntityId.Clear();
            foreach (var ec in _entityRegistry.Entities)
                IndexEntity(ec);
        }

        private void IndexEntity(EntityComponent ec)
        {
            if (ec == null || ec.GameObject == null)
                return;

            var entityId = ec.EntityId;
            if (entityId == Guid.Empty)
                return;

            int legacyId = ResolvePublicId(ec);
            if (legacyId == 0)
                return;

            _legacyToEntityId[legacyId] = entityId;
        }

        private void RemoveEntity(EntityComponent ec)
        {
            if (ec == null)
                return;

            var legacyId = ResolvePublicId(ec);
            if (legacyId != 0)
                _legacyToEntityId.Remove(legacyId);
        }

        [OnEvent]
        public void OnEntityInitialized(EntityInitializedEvent e)
        {
            IndexEntity(e.Entity);

            if (!HasWebhookSubscribers()) return;

            var ec = e.Entity;
            if (ec.GetComponent<Timberborn.Buildings.Building>() != null)
                WebhookMgr.PushEvent("building.placed", WebhookMgr.DataEntity(GetLegacyId(ec), CanonicalName(ec.GameObject.name)));
            else if (ec.GetComponent<Timberborn.NeedSystem.NeedManager>() != null)
                WebhookMgr.PushEvent("beaver.born", WebhookMgr.DataEntityBot(GetLegacyId(ec), CanonicalName(ec.GameObject.name), ec.GetComponent<Bot>() != null));
        }

        [OnEvent]
        public void OnEntityDeleted(EntityDeletedEvent e)
        {
            if (HasWebhookSubscribers())
            {
                var ec = e.Entity;
                if (ec.GetComponent<Timberborn.Buildings.Building>() != null)
                    WebhookMgr.PushEvent("building.demolished", WebhookMgr.DataEntity(GetLegacyId(ec), CanonicalName(ec.GameObject.name)));
                else if (ec.GetComponent<Timberborn.NeedSystem.NeedManager>() != null)
                    WebhookMgr.PushEvent("beaver.died", WebhookMgr.DataEntity(GetLegacyId(ec), CanonicalName(ec.GameObject.name)));
            }

            RemoveEntity(e.Entity);
        }

        // No subscribers ⇒ skip the DataEntity JW allocation. The check is
        // ~free (volatile read + ConcurrentDictionary.Count).
        private bool HasWebhookSubscribers()
        {
            var b = WebhookMgr?.Broadcaster;
            return b != null && b.ConnectionCount > 0;
        }
        private int ResolvePublicId(EntityComponent ec)
        {
            if (ec == null) return 0;
            var guid = ec.EntityId;
            if (guid != Guid.Empty)
            {
                // Use stable, deterministic XOR hash generated by Value-Type overridden GetHashCode().
                int h = Math.Abs(guid.GetHashCode());
                return h == 0 ? 1 : h;
            }
            // Strict deterministic model enforces that untracked GUIDs cannot resolve to valid integer identifiers.
            return 0;
        }
    }
}

